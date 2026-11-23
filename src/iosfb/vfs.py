"""The protocol-neutral virtual tree served to Windows.

Layout::

    /<Device name>/Media/...            AFC media area
    /<Device name>/Apps/<App name>/...  Documents folder of a file-sharing app

Names are made safe for Windows: characters Windows forbids in file names are
shown as their full-width look-alikes, and names that Windows could not tell
apart are disambiguated (devices, apps) or shown once (files). Lookups ignore
case, as Windows does. Listings are cached for a few seconds so that
Explorer's bursts of repeated requests do not each hit the device.
"""

from __future__ import annotations

import logging
import posixpath
import threading
import time
import uuid
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from typing import Generic, TypeVar

from .backend.base import (
    MEDIA_ROOT,
    AppInfo,
    BackendError,
    DeviceBackend,
    DeviceGone,
    DeviceInfo,
    Entry,
    FileReader,
    FileWriter,
    NotAllowed,
    NotFound,
)

log = logging.getLogger(__name__)

APPS_DIR = "Apps"
MEDIA_DIR = "Media"

#: New content is written under a name starting with this, next to its
#: destination, and renamed into place once complete. A failed upload then
#: never leaves a half-written file under the real name.
PART_PREFIX = ".iosfb-part-"
COPY_BLOCK = 1024 * 1024

_ILLEGAL = '\\:*?"<>|'
_REPLACEMENT = "＼：＊？＂＜＞｜"
_SANITIZE = str.maketrans(
    {**dict(zip(_ILLEGAL, _REPLACEMENT)), "/": "／", **{chr(c): "_" for c in range(32)}}
)

T = TypeVar("T")


def sanitize(name: str) -> str:
    """Return ``name`` as it is shown to Windows."""
    shown = name.translate(_SANITIZE).rstrip(" .")
    return shown or "_"


class _Names(Generic[T]):
    """Shown name -> item, looked up exactly first and then ignoring case."""

    def __init__(self) -> None:
        self.items: dict[str, T] = {}
        self._folded: dict[str, str] = {}

    def add(self, shown: str, item: T) -> bool:
        """Add unless Windows could not tell ``shown`` from an existing name."""
        key = shown.casefold()
        if key in self._folded:
            return False
        self._folded[key] = shown
        self.items[shown] = item
        return True

    def find(self, shown: str) -> T:
        try:
            return self.items[self._folded[shown.casefold()]]
        except KeyError:
            raise NotFound(shown) from None


def _unique_names(items: list[T], name: Callable[[T], str], suffix: Callable[[T], str]) -> _Names[T]:
    """Give every item its own display name, adding a suffix where names collide."""
    counts: dict[str, int] = {}
    for item in items:
        key = sanitize(name(item)).casefold()
        counts[key] = counts.get(key, 0) + 1
    out: _Names[T] = _Names()
    for item in items:
        shown = sanitize(name(item))
        if counts[shown.casefold()] > 1:
            shown = f"{shown} ({sanitize(suffix(item))})"
        candidate, n = shown, 2
        while not out.add(candidate, item):  # the suffixed name can collide too
            candidate, n = f"{shown} ({n})", n + 1
    return out


@dataclass(frozen=True)
class _Folder:
    """One device folder: what is shown, and the device's own name for each entry."""

    shown: list[Entry]
    real: _Names[str]


@dataclass(frozen=True)
class _Target:
    """A place on a device that a change applies to."""

    udid: str
    root: str
    parent: str  # the device's path of the containing folder
    name: str  # the device's name, or the new name if nothing is there yet
    entry: Entry | None  # what is there now

    @property
    def path(self) -> str:
        return posixpath.join(self.parent, self.name)

    def sibling(self) -> str:
        """A fresh temporary path in the same folder."""
        return posixpath.join(self.parent, PART_PREFIX + uuid.uuid4().hex[:12])


class _Upload:
    """Writes to a temporary file; ``close`` moves it into place, ``abort`` discards it."""

    def __init__(self, vfs: Vfs, target: _Target, temp: str, writer: FileWriter) -> None:
        self._vfs = vfs
        self._target = target
        self._temp = temp
        self._writer = writer
        self._done = False

    def write(self, data: bytes) -> None:
        self._vfs._guard(lambda: self._writer.write(data))

    def close(self) -> None:
        if self._done:
            return
        t = self._target
        try:
            self._vfs._guard(self._writer.close)
            self._vfs._guard(lambda: self._vfs._backend.rename(t.udid, t.root, self._temp, t.path))
        except BackendError:
            self.abort()
            raise
        self._done = True
        self._vfs._forget(t.udid, t.root)

    def abort(self) -> None:
        if self._done:
            return
        self._done = True
        t = self._target
        self._vfs._quietly(self._writer.close)
        self._vfs._quietly(lambda: self._vfs._backend.remove(t.udid, t.root, self._temp))
        self._vfs._forget(t.udid, t.root)


class Vfs:
    def __init__(
        self,
        backend: DeviceBackend,
        cache_ttl: float = 5.0,
        cache_size: int = 2048,
        clock: Callable[[], float] = time.monotonic,
        writable: bool = False,
    ) -> None:
        self.writable = writable
        self._backend = backend
        self._ttl = cache_ttl
        self._cache_size = cache_size
        self._clock = clock
        self._cache: OrderedDict[tuple, tuple[float, object]] = OrderedDict()
        self._loading: dict[tuple, threading.Lock] = {}
        self._lock = threading.Lock()
        #: Date given to folders that exist only in this tree, and to files
        #: whose date is unknown: Windows shows errors for entries without one.
        self.born = time.time()

    # -- public API ---------------------------------------------------------

    def listdir(self, vpath: str) -> list[Entry]:
        parts = _split(vpath)
        if not parts:
            return [self._virtual(name) for name in self._devices().items]
        device = self._devices().find(parts[0])
        if len(parts) == 1:
            return [self._virtual(APPS_DIR), self._virtual(MEDIA_DIR)]
        if len(parts) == 2 and _same(parts[1], APPS_DIR):
            return [self._virtual(name) for name in self._apps(device.udid).items]
        root, rest = self._root(device, parts)
        return self._folder(device.udid, root, self._real_path(device.udid, root, rest)).shown

    def stat(self, vpath: str) -> Entry:
        parts = _split(vpath)
        if not parts:
            return self._virtual("")
        device = self._devices().find(parts[0])
        if len(parts) == 1:
            return self._virtual(parts[0])
        if len(parts) == 2 and _same(parts[1], APPS_DIR):
            return self._virtual(APPS_DIR)
        root, rest = self._root(device, parts)
        if not rest:
            return self._virtual(parts[-1])
        parent = self._folder(device.udid, root, self._real_path(device.udid, root, rest[:-1]))
        wanted = rest[-1].casefold()
        for entry in parent.shown:
            if entry.name.casefold() == wanted:
                return entry
        raise NotFound(vpath)

    def open(self, vpath: str) -> FileReader:
        parts = _split(vpath)
        if len(parts) < 3:
            raise NotFound(vpath)
        device = self._devices().find(parts[0])
        root, rest = self._root(device, parts)
        if not rest:
            raise NotFound(vpath)
        real = self._real_path(device.udid, root, rest)
        return self._guard(lambda: self._backend.open_read(device.udid, root, real))

    def invalidate(self) -> None:
        with self._lock:
            self._cache.clear()

    # -- changes (only when writable) ---------------------------------------

    def create(self, vpath: str) -> _Upload:
        """Open a file for writing, creating it or replacing its content.

        Call ``close()`` on the result to keep what was written, ``abort()`` to discard it.
        """
        target = self._target(vpath)
        if target.entry is not None and target.entry.is_dir:
            raise NotAllowed(f"{vpath} is a folder")
        temp = target.sibling()
        writer = self._guard(lambda: self._backend.open_write(target.udid, target.root, temp))
        return _Upload(self, target, temp, writer)

    def mkdir(self, vpath: str) -> None:
        target = self._target(vpath)
        if target.entry is not None:
            raise NotAllowed(f"{vpath} already exists")
        self._change(target, lambda: self._backend.mkdir(target.udid, target.root, target.path))

    def delete(self, vpath: str) -> None:
        """Delete a file, or a folder and everything in it."""
        target = self._existing(vpath)
        self._change(target, lambda: self._backend.remove(target.udid, target.root, target.path))

    def move(self, source: str, destination: str) -> None:
        """Rename or move. An existing file at the destination is replaced; a folder is not."""
        src, dst = self._pair(source, destination)
        if (src.udid, src.root) == (dst.udid, dst.root):
            if src.path != dst.path:
                self._change(dst, lambda: self._backend.rename(src.udid, src.root, src.path, dst.path))
            return
        # Another app or another device: there is no rename across those.
        self._copy_into_place(src, dst)
        self._change(src, lambda: self._backend.remove(src.udid, src.root, src.path))

    def copy(self, source: str, destination: str) -> None:
        src, dst = self._pair(source, destination)
        if (src.udid, src.root, src.path) != (dst.udid, dst.root, dst.path):
            self._copy_into_place(src, dst)

    def set_mtime(self, vpath: str, mtime: float) -> None:
        target = self._existing(vpath)
        self._change(target, lambda: self._backend.set_mtime(target.udid, target.root, target.path, mtime))

    def _target(self, vpath: str) -> _Target:
        if not self.writable:
            raise NotAllowed("the share is read-only")
        parts = _split(vpath)
        fixed = len(parts) < 3 or (len(parts) == 3 and _same(parts[1], APPS_DIR))
        if fixed:
            raise NotAllowed(f"{vpath} is part of the share's layout and cannot be changed")
        device = self._devices().find(parts[0])
        root, rest = self._root(device, parts)
        parent = self._real_path(device.udid, root, rest[:-1])
        folder = self._folder(device.udid, root, parent)
        wanted = rest[-1].casefold()
        for entry in folder.shown:
            if entry.name.casefold() == wanted:
                return _Target(device.udid, root, parent, folder.real.find(entry.name), entry)
        return _Target(device.udid, root, parent, _new_name(rest[-1]), None)

    def _existing(self, vpath: str) -> _Target:
        target = self._target(vpath)
        if target.entry is None:
            raise NotFound(vpath)
        return target

    def _pair(self, source: str, destination: str) -> tuple[_Target, _Target]:
        src = self._existing(source)
        dst = self._target(destination)
        if dst.entry is not None and (src.udid, src.root, src.path) == (dst.udid, dst.root, dst.path):
            # The same entry under another spelling: a change of letter case.
            dst = _Target(dst.udid, dst.root, dst.parent, _new_name(_split(destination)[-1]), None)
        elif dst.entry is not None and (dst.entry.is_dir or src.entry.is_dir):
            raise NotAllowed(f"{destination} already exists")
        return src, dst

    def _change(self, target: _Target, call: Callable[[], None]) -> None:
        try:
            self._guard(call)
        finally:
            self._forget(target.udid, target.root)

    def _copy_into_place(self, src: _Target, dst: _Target) -> None:
        """Copy to a temporary name beside the destination, then rename it into place."""
        temp = dst.sibling()
        try:
            self._guard(lambda: self._copy(src.udid, src.root, src.path, src.entry.is_dir, dst.udid, dst.root, temp))
            self._guard(lambda: self._backend.rename(dst.udid, dst.root, temp, dst.path))
        except BackendError:
            self._quietly(lambda: self._backend.remove(dst.udid, dst.root, temp))
            raise
        finally:
            self._forget(dst.udid, dst.root)

    def _copy(self, udid: str, root: str, path: str, is_dir: bool, to_udid: str, to_root: str, to_path: str) -> None:
        backend = self._backend
        if is_dir:
            backend.mkdir(to_udid, to_root, to_path)
            for child in backend.listdir(udid, root, path):
                self._copy(
                    udid, root, posixpath.join(path, child.name), child.is_dir,
                    to_udid, to_root, posixpath.join(to_path, child.name),
                )  # fmt: skip
            return
        reader = backend.open_read(udid, root, path)
        try:
            writer = backend.open_write(to_udid, to_root, to_path)
            try:
                offset = 0
                while block := reader.read_at(offset, COPY_BLOCK):
                    writer.write(block)
                    offset += len(block)
            except BaseException:
                self._quietly(writer.close)
                raise
            writer.close()
        finally:
            self._quietly(reader.close)

    def _forget(self, udid: str, root: str) -> None:
        """Drop the cached listings of one root after a change to it."""
        with self._lock:
            for key in [k for k in self._cache if k[:3] == ("ls", udid, root)]:
                del self._cache[key]

    def _quietly(self, call: Callable[[], object]) -> None:
        try:
            self._guard(call)
        except BackendError:
            log.debug("cleanup failed", exc_info=True)

    # -- resolution ---------------------------------------------------------

    def _virtual(self, name: str) -> Entry:
        return Entry(name, True, 0, self.born)

    def _devices(self) -> _Names[DeviceInfo]:
        return self._cached(
            ("devices",),
            lambda: _unique_names(self._backend.list_devices(), lambda d: d.name or d.udid, lambda d: d.udid[-4:]),
        )

    def _apps(self, udid: str) -> _Names[AppInfo]:
        def load() -> _Names[AppInfo]:
            apps = sorted(self._backend.list_apps(udid), key=lambda a: (a.name.casefold(), a.bundle_id))
            return _unique_names(apps, lambda a: a.name or a.bundle_id, lambda a: a.bundle_id)

        return self._cached(("apps", udid), load)

    def _root(self, device: DeviceInfo, parts: list[str]) -> tuple[str, list[str]]:
        """Return the backend root and the remaining (shown) path components."""
        if _same(parts[1], MEDIA_DIR):
            return MEDIA_ROOT, parts[2:]
        if _same(parts[1], APPS_DIR) and len(parts) >= 3:
            return self._apps(device.udid).find(parts[2]).bundle_id, parts[3:]
        raise NotFound("/".join(parts))

    def _real_path(self, udid: str, root: str, shown_parts: list[str]) -> str:
        """Translate shown names to the device's names, one folder at a time.

        Every component goes through its parent's listing. That keeps listing,
        stat and open in agreement about which device file a shown name means,
        and nothing that the device did not list is ever sent to it.
        """
        real: list[str] = []
        for shown in shown_parts:
            real.append(self._folder(udid, root, "/" + "/".join(real)).real.find(shown))
        return "/" + "/".join(real)

    def _folder(self, udid: str, root: str, path: str) -> _Folder:
        def load() -> _Folder:
            shown: list[Entry] = []
            real: _Names[str] = _Names()
            for entry in self._backend.listdir(udid, root, path):
                name = sanitize(entry.name)
                if not real.add(name, entry.name):
                    continue  # Windows cannot tell these apart; show the first only
                shown.append(entry if name == entry.name else Entry(name, entry.is_dir, entry.size, entry.mtime))
            return _Folder(shown, real)

        return self._cached(("ls", udid, root, path), load)

    # -- cache --------------------------------------------------------------

    def _cached(self, key: tuple, load: Callable[[], T]) -> T:
        hit = self._peek(key)
        if hit is not None:
            return hit[0]
        with self._lock:
            loading = self._loading.setdefault(key, threading.Lock())
        with loading:  # one load per key; the others wait and reuse the result
            hit = self._peek(key)
            if hit is not None:
                return hit[0]
            try:
                value = self._guard(load)
            finally:
                with self._lock:
                    self._loading.pop(key, None)
            with self._lock:
                # Stamped after the load, so a slow listing is still cached.
                self._cache[key] = (self._clock() + self._ttl, value)
                self._cache.move_to_end(key)
                while len(self._cache) > self._cache_size:
                    self._cache.popitem(last=False)
            return value

    def _peek(self, key: tuple) -> tuple[object] | None:
        with self._lock:
            hit = self._cache.get(key)
            if hit is not None and hit[0] > self._clock():
                self._cache.move_to_end(key)
                return (hit[1],)
        return None

    def _guard(self, call: Callable[[], T]) -> T:
        """Run a backend call; forget everything cached if the device vanished."""
        try:
            return call()
        except DeviceGone:
            self.invalidate()
            raise


def _same(a: str, b: str) -> bool:
    return a.casefold() == b.casefold()


def _new_name(name: str) -> str:
    """Check a name a client wants to create; it is used on the device as given."""
    bad = not name or "/" in name or any(ord(c) < 32 for c in name) or len(name.encode("utf-8")) > 255
    if bad or name.startswith(PART_PREFIX):
        raise NotAllowed(f"{name!r} cannot be used as a name")
    return name


def _split(vpath: str) -> list[str]:
    parts = [p for p in vpath.split("/") if p]
    for part in parts:
        if part in (".", "..") or "\\" in part:
            raise NotFound(vpath)  # never a real shown name; refuse traversal outright
    return parts
