"""In-memory backend used by the tests and by ``iosfb serve --demo``.

A tree is a nested dict: a ``dict`` value is a directory, a ``bytes`` value is
a file.
"""

from __future__ import annotations

import posixpath
import threading
from dataclasses import dataclass, field

from .base import MEDIA_ROOT, AppInfo, BackendError, DeviceGone, DeviceInfo, Entry, NotAllowed, NotFound

Tree = dict[str, "Tree | bytes"]


@dataclass
class MockDevice:
    info: DeviceInfo
    media: Tree = field(default_factory=dict)
    apps: dict[str, tuple[str, Tree]] = field(default_factory=dict)  # bundle id -> (name, tree)
    connected: bool = True
    mtime: float = 1_700_000_000.0


class _MockReader:
    def __init__(self, backend: MockBackend, udid: str, data: bytes) -> None:
        self._backend = backend
        self._udid = udid
        self._data = data

    def read_at(self, offset: int, size: int) -> bytes:
        self._backend._device(self._udid)  # raises DeviceGone after an unplug
        self._backend.read_calls.append((offset, size))
        return self._data[offset : offset + size]

    def close(self) -> None:
        self._backend.open_readers -= 1


class _MockWriter:
    def __init__(self, backend: MockBackend, udid: str, parent: Tree, name: str) -> None:
        self._backend = backend
        self._udid = udid
        self._parent = parent
        self._name = name

    def write(self, data: bytes) -> None:
        self._backend._device(self._udid)
        if self._backend.write_error is not None:
            raise self._backend.write_error
        self._parent[self._name] += bytes(data)

    def close(self) -> None:
        if self._name is None:
            return
        self._name = None
        self._backend.open_writers -= 1
        self._backend._device(self._udid)


class MockBackend:
    def __init__(self, devices: list[MockDevice] | None = None) -> None:
        self.open_writers = 0
        self.write_error: BackendError | None = None  # raised by every write while set
        self.mtimes: dict[tuple[str, str, str], float] = {}  # set by set_mtime
        self.devices: dict[str, MockDevice] = {d.info.udid: d for d in devices or []}
        self.calls: list[tuple] = []  # every backend call, for cache tests
        self.read_calls: list[tuple[int, int]] = []
        self.open_readers = 0
        self._lock = threading.Lock()

    # -- test helpers -------------------------------------------------------

    def unplug(self, udid: str) -> None:
        self.devices[udid].connected = False

    def replug(self, udid: str) -> None:
        self.devices[udid].connected = True

    # -- DeviceBackend ------------------------------------------------------

    def list_devices(self) -> list[DeviceInfo]:
        self._record("list_devices")
        return [d.info for d in self.devices.values() if d.connected]

    def list_apps(self, udid: str) -> list[AppInfo]:
        self._record("list_apps", udid)
        dev = self._device(udid)
        return [AppInfo(bundle_id, name) for bundle_id, (name, _) in dev.apps.items()]

    def listdir(self, udid: str, root: str, path: str) -> list[Entry]:
        self._record("listdir", udid, root, path)
        node = self._node(udid, root, path)
        if not isinstance(node, dict):
            raise NotFound(path)
        base = path.rstrip("/")
        return [self._entry(name, child, self._mtime(udid, root, f"{base}/{name}")) for name, child in node.items()]

    def stat(self, udid: str, root: str, path: str) -> Entry:
        self._record("stat", udid, root, path)
        node = self._node(udid, root, path)
        return self._entry(posixpath.basename(path.rstrip("/")), node, self._mtime(udid, root, path))

    def open_read(self, udid: str, root: str, path: str) -> _MockReader:
        self._record("open_read", udid, root, path)
        node = self._node(udid, root, path)
        if isinstance(node, dict):
            raise NotFound(path)
        self.open_readers += 1
        return _MockReader(self, udid, node)

    def open_write(self, udid: str, root: str, path: str) -> _MockWriter:
        self._record("open_write", udid, root, path)
        parent, name = self._parent(udid, root, path)
        if isinstance(parent.get(name), dict):
            raise BackendError(f"{path} is a folder")
        parent[name] = b""
        self.open_writers += 1
        return _MockWriter(self, udid, parent, name)

    def mkdir(self, udid: str, root: str, path: str) -> None:
        self._record("mkdir", udid, root, path)
        parent, name = self._parent(udid, root, path)
        if name in parent:
            raise BackendError(f"{path} exists")
        parent[name] = {}

    def remove(self, udid: str, root: str, path: str) -> None:
        self._record("remove", udid, root, path)
        parent, name = self._parent(udid, root, path)
        if name not in parent:
            raise NotFound(path)
        del parent[name]

    def rename(self, udid: str, root: str, source: str, target: str) -> None:
        self._record("rename", udid, root, source, target)
        src_parent, src_name = self._parent(udid, root, source)
        dst_parent, dst_name = self._parent(udid, root, target)
        if src_name not in src_parent:
            raise NotFound(source)
        if isinstance(dst_parent.get(dst_name), dict):
            raise BackendError(f"{target} is a folder")
        dst_parent[dst_name] = src_parent.pop(src_name)

    def set_mtime(self, udid: str, root: str, path: str, mtime: float) -> None:
        self._record("set_mtime", udid, root, path)
        self._node(udid, root, path)
        self.mtimes[udid, root, "/" + path.strip("/")] = mtime

    def close(self) -> None:
        pass

    # -- internals ----------------------------------------------------------

    def _record(self, *call) -> None:
        with self._lock:
            self.calls.append(call)

    def _device(self, udid: str) -> MockDevice:
        dev = self.devices.get(udid)
        if dev is None:
            raise NotFound(udid)
        if not dev.connected:
            raise DeviceGone(udid)
        return dev

    def _node(self, udid: str, root: str, path: str) -> Tree | bytes:
        dev = self._device(udid)
        if root == MEDIA_ROOT:
            node: Tree | bytes = dev.media
        elif root in dev.apps:
            node = dev.apps[root][1]
        else:
            raise NotFound(root)
        for part in (p for p in path.split("/") if p):
            if not isinstance(node, dict) or part not in node:
                raise NotFound(path)
            node = node[part]
        return node

    def _parent(self, udid: str, root: str, path: str) -> tuple[Tree, str]:
        path = path.strip("/")
        if not path:
            raise NotAllowed("the root cannot be changed")
        parent = self._node(udid, root, posixpath.dirname(path))
        if not isinstance(parent, dict):
            raise NotFound(path)
        return parent, posixpath.basename(path)

    def _mtime(self, udid: str, root: str, path: str) -> float:
        return self.mtimes.get((udid, root, "/" + path.strip("/")), self.devices[udid].mtime)

    @staticmethod
    def _entry(name: str, node: Tree | bytes, mtime: float) -> Entry:
        if isinstance(node, dict):
            return Entry(name, True, 0, mtime)
        return Entry(name, False, len(node), mtime)


def demo_backend() -> MockBackend:
    """A small fake device, so the server can be tried without an iPhone."""
    phone = MockDevice(
        info=DeviceInfo("00008130-000A11112222001C", "Demo iPhone", "iPhone", "iPhone16,1", "27.2"),
        media={
            "DCIM": {"100APPLE": {"IMG_0001.JPG": b"\xff\xd8\xff demo jpeg", "IMG_0002.MOV": b"demo" * 4096}},
            "Downloads": {},
            "Recordings": {"memo.m4a": b"demo audio"},
        },
        apps={
            "org.videolan.vlc-ios": ("VLC", {"movie.mkv": b"\x1aE\xdf\xa3" + b"\0" * 65536, "Subs": {"movie.srt": b"1\n"}}),
            "com.example.notes": ("Notes: Pro", {"todo.txt": b"buy milk\n"}),
        },
    )
    return MockBackend([phone])
