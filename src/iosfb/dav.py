"""WebDAV front end: a WsgiDAV provider over the virtual tree (read-only unless the tree is writable)."""

from __future__ import annotations

import contextlib
import logging
import mimetypes
import posixpath

from wsgidav import util
from wsgidav.dav_error import HTTP_FORBIDDEN, HTTP_NOT_FOUND, DAVError
from wsgidav.dav_provider import DAVCollection, DAVNonCollection, DAVProvider

from .backend.base import BackendError, DeviceGone, Entry, FileReader, NotAllowed, NotFound
from .vfs import Vfs

log = logging.getLogger(__name__)

HTTP_SERVICE_UNAVAILABLE = 503

# How much to fetch from the device per request. WsgiDAV asks for small blocks;
# reading ahead keeps the number of round trips over USB low.
READ_AHEAD = 1024 * 1024


class DeviceStream:
    """A seekable, read-only file object over a positional ``FileReader``.

    At most ``READ_AHEAD`` bytes (or one oversized read) are held in memory.
    """

    def __init__(self, reader: FileReader, size: int) -> None:
        self._reader = reader
        self._size = size
        self._pos = 0
        self._buf = b""
        self._buf_start = 0
        self._closed = False

    def seek(self, pos: int, whence: int = 0) -> int:
        if whence == 1:
            pos += self._pos
        elif whence == 2:
            pos += self._size
        self._pos = max(0, pos)
        return self._pos

    def tell(self) -> int:
        return self._pos

    def read(self, size: int = -1) -> bytes:
        if size is None or size < 0:
            size = max(0, self._size - self._pos)
        out = bytearray()
        while len(out) < size:
            offset = self._pos - self._buf_start
            if not 0 <= offset < len(self._buf):
                self._buf = self._reader.read_at(self._pos, max(READ_AHEAD, min(size - len(out), 4 * READ_AHEAD)))
                self._buf_start = self._pos
                offset = 0
                if not self._buf:
                    break  # end of file
            chunk = self._buf[offset : offset + size - len(out)]
            out += chunk
            self._pos += len(chunk)
        return bytes(out)

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            self._buf = b""
            self._reader.close()


def _dav_error(exc: BackendError) -> DAVError:
    if isinstance(exc, DeviceGone):
        return DAVError(HTTP_SERVICE_UNAVAILABLE, "The device is not connected.")
    return DAVError(HTTP_SERVICE_UNAVAILABLE, f"Device error: {exc}")


@contextlib.contextmanager
def _changing():
    """Turn failures of a change into the HTTP errors a client expects."""
    try:
        yield
    except NotAllowed as exc:
        raise DAVError(HTTP_FORBIDDEN, str(exc)) from exc
    except NotFound as exc:
        raise DAVError(HTTP_NOT_FOUND) from exc
    except DeviceGone as exc:
        raise _dav_error(exc) from exc
    except BackendError as exc:  # the device said no: no space, a protected folder, ...
        raise DAVError(HTTP_FORBIDDEN, f"The device refused the change: {exc}") from exc


class _Sink:
    """The file object WsgiDAV writes an upload to."""

    def __init__(self, upload) -> None:
        self._upload = upload

    def write(self, data: bytes) -> None:
        with _changing():
            self._upload.write(data)

    def close(self) -> None:
        pass  # the upload is finished or discarded in end_write()


class _Changeable:
    """DELETE, MOVE, COPY and timestamp changes, shared by folders and files."""

    path: str
    _vfs: Vfs

    def handle_delete(self) -> bool:
        with _changing():
            self._vfs.delete(self.path)
        return True

    def handle_move(self, dest_path: str) -> bool:
        with _changing():
            self._vfs.move(self.path, dest_path)
        return True

    def handle_copy(self, dest_path: str, *, depth_infinity: bool) -> bool:
        with _changing():
            if self._entry.is_dir and not depth_infinity:
                self._vfs.mkdir(dest_path)
            else:
                self._vfs.copy(self.path, dest_path)
        return True

    def set_last_modified(self, dest_path: str, time_stamp, *, dry_run: bool) -> bool:
        """Windows sends the original file date after an upload; pass it on to the device."""
        if not self._vfs.writable:
            raise DAVError(HTTP_FORBIDDEN)
        seconds = time_stamp if isinstance(time_stamp, (int, float)) else util.parse_time_string(str(time_stamp))
        if seconds is None:
            raise DAVError(HTTP_FORBIDDEN, "Unreadable date.")
        if not dry_run:
            try:
                self._vfs.set_mtime(dest_path, float(seconds))
            except BackendError:
                log.debug("setting the date of %s failed", dest_path, exc_info=True)
        return True


class _Folder(_Changeable, DAVCollection):
    def __init__(self, path: str, environ: dict, vfs: Vfs, entry: Entry) -> None:
        super().__init__(path, environ)
        self._vfs = vfs
        self._entry = entry

    def _entries(self) -> list[Entry]:
        try:
            return self._vfs.listdir(self.path)
        except NotFound:
            return []
        except BackendError as exc:
            raise _dav_error(exc) from exc

    def get_member_names(self) -> list[str]:
        return [e.name for e in self._entries()]

    def get_member_list(self) -> list:
        # Built from one listing, so a PROPFIND costs one device round trip
        # rather than one per child.
        return [_resource(posixpath.join(self.path, e.name), self.environ, self._vfs, e) for e in self._entries()]

    def get_member(self, name: str):
        return self.provider.get_resource_inst(posixpath.join(self.path, name), self.environ)

    def create_empty_resource(self, name: str):
        path = posixpath.join(self.path, name)
        with _changing():
            self._vfs.create(path).close()
        return self.provider.get_resource_inst(path, self.environ)

    def create_collection(self, name: str) -> None:
        with _changing():
            self._vfs.mkdir(posixpath.join(self.path, name))

    def get_display_name(self) -> str:
        return self._entry.name or "iPhone"

    def get_last_modified(self):
        return self._entry.mtime or self._vfs.born

    def get_creation_date(self):
        return self._entry.mtime or self._vfs.born

    def support_etag(self) -> bool:
        return False

    def get_etag(self):
        return None


class _File(_Changeable, DAVNonCollection):
    def __init__(self, path: str, environ: dict, vfs: Vfs, entry: Entry) -> None:
        super().__init__(path, environ)
        self._vfs = vfs
        self._entry = entry
        self._upload = None

    def get_content_length(self) -> int:
        return self._entry.size

    def get_content_type(self) -> str:
        return mimetypes.guess_type(self._entry.name)[0] or "application/octet-stream"

    def get_display_name(self) -> str:
        return self._entry.name

    def get_last_modified(self):
        return self._entry.mtime or self._vfs.born

    def get_creation_date(self):
        return self._entry.mtime or self._vfs.born

    def support_etag(self) -> bool:
        # Size alone cannot tell two versions apart, so no ETag without a date.
        return self._entry.mtime is not None

    def get_etag(self) -> str | None:
        if self._entry.mtime is None:
            return None
        return f"{self._entry.size:x}-{int(self._entry.mtime * 1000):x}"

    def support_ranges(self) -> bool:
        return True

    def get_content(self) -> DeviceStream:
        try:
            return DeviceStream(self._vfs.open(self.path), self._entry.size)
        except NotFound as exc:
            raise DAVError(404) from exc
        except BackendError as exc:
            raise _dav_error(exc) from exc

    def begin_write(self, *, content_type=None) -> _Sink:
        with _changing():
            self._upload = self._vfs.create(self.path)
        return _Sink(self._upload)

    def end_write(self, *, with_errors: bool) -> None:
        upload, self._upload = self._upload, None
        if upload is None:
            return
        if with_errors:
            upload.abort()
            return
        with _changing():
            upload.close()
            self._entry = self._vfs.stat(self.path)  # so the reply carries the new ETag


def _resource(path: str, environ: dict, vfs: Vfs, entry: Entry):
    cls = _Folder if entry.is_dir else _File
    return cls(path, environ, vfs, entry)


class VfsProvider(DAVProvider):
    def __init__(self, vfs: Vfs) -> None:
        super().__init__()
        self._vfs = vfs

    def is_readonly(self) -> bool:
        return not self._vfs.writable

    def get_resource_inst(self, path: str, environ: dict):
        path = "/" + path.strip("/")
        try:
            entry = self._vfs.stat(path)
        except NotFound:
            return None
        except BackendError as exc:
            raise _dav_error(exc) from exc
        return _resource(path, environ, self._vfs, entry)
