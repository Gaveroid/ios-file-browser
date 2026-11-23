"""The interface every device backend implements.

A backend exposes, per device, a set of *roots*. Each root is an independent
file tree: ``MEDIA_ROOT`` is the AFC media area, and every other root is the
bundle id of an app whose Documents folder is shared. Paths inside a root are
POSIX style and start with ``/``; ``/`` is the top of the root.

All methods are synchronous and must be safe to call from several threads.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

MEDIA_ROOT = "media"


class BackendError(Exception):
    """Base class for backend failures."""


class NotFound(BackendError):
    """The device, root or path does not exist."""


class DeviceGone(BackendError):
    """The device was unplugged or stopped answering."""


@dataclass(frozen=True)
class DeviceInfo:
    udid: str
    name: str
    device_class: str = ""
    product_type: str = ""
    os_version: str = ""


@dataclass(frozen=True)
class AppInfo:
    bundle_id: str
    name: str


@dataclass(frozen=True)
class Entry:
    name: str
    is_dir: bool
    size: int = 0
    mtime: float | None = None  # seconds since the Unix epoch


class FileReader(Protocol):
    """An open file. Reads are positional, so one reader can serve Range requests."""

    def read_at(self, offset: int, size: int) -> bytes:
        """Return up to ``size`` bytes from ``offset``; empty bytes at end of file."""

    def close(self) -> None: ...


class NotAllowed(BackendError):
    """The change is refused: the share is read-only, or the target cannot be changed."""


class FileWriter(Protocol):
    """A file open for writing from its start. ``close`` must be called to finish it."""

    def write(self, data: bytes) -> None: ...

    def close(self) -> None: ...


class DeviceBackend(Protocol):
    def list_devices(self) -> list[DeviceInfo]: ...

    def list_apps(self, udid: str) -> list[AppInfo]:
        """Apps on the device whose Documents folder is reachable."""

    def listdir(self, udid: str, root: str, path: str) -> list[Entry]: ...

    def stat(self, udid: str, root: str, path: str) -> Entry: ...

    def open_read(self, udid: str, root: str, path: str) -> FileReader: ...

    def open_write(self, udid: str, root: str, path: str) -> FileWriter:
        """Create ``path`` (or empty it) and return a writer for its content."""

    def mkdir(self, udid: str, root: str, path: str) -> None: ...

    def remove(self, udid: str, root: str, path: str) -> None:
        """Delete a file, or a folder with everything in it. Never the root itself."""

    def rename(self, udid: str, root: str, source: str, target: str) -> None:
        """Rename or move inside one root. An existing file at ``target`` is replaced."""

    def set_mtime(self, udid: str, root: str, path: str, mtime: float) -> None: ...

    def close(self) -> None: ...
