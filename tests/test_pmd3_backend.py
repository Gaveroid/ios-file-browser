"""The real backend's logic, exercised with a fake in place of pymobiledevice3's device calls.

Only the library's exception classes are real here; no device or usbmuxd is used.
"""

import asyncio
import threading
import time

import pytest
from pymobiledevice3 import exceptions as pmd3_exc

from iosfb.backend.base import MEDIA_ROOT, AppInfo, BackendError, DeviceGone, DeviceInfo, NotFound
from iosfb.backend.pmd3 import Pmd3Backend

UDID = "00008130-000000000000ABCD"
NS = 10**9


def not_found(path):
    return pmd3_exc.AfcFileNotFoundError(f"missing: {path}", 8, path)


class FakeAfc:
    """A tree of {path: bytes | None}; None marks a directory."""

    def __init__(self, files):
        self.files = files
        self.closed = False
        self.fail: Exception | None = None  # raised by every call while set
        self.hang = False  # listdir never answers while set
        self.handles = {}

    def _check(self, path=None):
        if self.fail:
            raise self.fail
        if path is not None and path not in self.files:
            raise not_found(path)

    async def listdir(self, path):
        while self.hang:
            await asyncio.sleep(0.01)
        self._check(path)
        prefix = path.rstrip("/") + "/"
        return [p[len(prefix) :] for p in self.files if p.startswith(prefix) and "/" not in p[len(prefix) :]]

    async def resolve_path(self, path):
        return path

    async def fopen(self, path, mode="r"):
        if mode == "w" and not self.fail:
            self.files[path] = b""
        self._check(path)
        handle = len(self.handles) + 1
        self.handles[handle] = path
        return handle

    async def pread(self, handle, size, offset):
        self._check()
        return self.files[self.handles[handle]][offset : offset + size]

    async def fclose(self, handle):
        self._check()
        del self.handles[handle]

    async def fwrite(self, handle, data):
        self._check()
        self.files[self.handles[handle]] += data

    async def makedirs(self, path):
        self._check()
        self.files[path] = None

    async def rm(self, path):
        self._check(path)
        for known in [p for p in self.files if p == path or p.startswith(path + "/")]:
            del self.files[known]

    async def rename(self, source, target):
        self._check(source)
        self.files[target] = self.files.pop(source)

    async def set_file_time(self, path, when):
        self._check(path)
        self.times = {**getattr(self, "times", {}), path: when}


class FakeApi:
    exc = pmd3_exc

    def __init__(self):
        self.present = [UDID]
        self.lockdown_error: Exception | None = None
        self.delay = 0.0  # seconds every connection attempt takes
        self.documents_error: Exception | None = None
        self.media = {
            "/": None,
            "/DCIM": None,
            "/DCIM/a.jpg": b"0123456789",
            "/DCIM/odd-date.bin": b"xy",
        }
        self.documents = {"/Documents": None, "/Documents/note.txt": b"hello", "/Documents/Sub": None}
        self.times = {"/DCIM/a.jpg": str(1_700_000_000 * NS), "/DCIM/odd-date.bin": "-62135596800000000000"}
        self.opened: list[str] = []
        self.afcs: list[FakeAfc] = []
        self.closed_lockdowns = 0

    async def udids(self):
        return list(self.present)

    async def open_lockdown(self, udid):
        await asyncio.sleep(self.delay)
        if self.lockdown_error:
            raise self.lockdown_error
        if udid not in self.present:
            raise pmd3_exc.NoDeviceConnectedError()
        self.opened.append("lockdown")
        return object()

    async def close_lockdown(self, lockdown):
        self.closed_lockdowns += 1

    def device_info(self, udid, lockdown):
        return DeviceInfo(udid, "Test iPhone", "iPhone", "iPhone16,1", "27.2")

    async def browse_apps(self, lockdown):
        return [
            {"CFBundleIdentifier": "com.shared", "CFBundleDisplayName": "Shared", "UIFileSharingEnabled": True},
            {"CFBundleIdentifier": "com.private", "CFBundleDisplayName": "Private"},
            {"CFBundleIdentifier": "com.noname", "UIFileSharingEnabled": True},
        ]

    async def open_media(self, lockdown):
        await asyncio.sleep(self.delay)
        return self._open("media", self.media)

    async def open_documents(self, lockdown, bundle_id):
        await asyncio.sleep(self.delay)
        if self.documents_error:
            raise self.documents_error
        if bundle_id != "com.shared":
            raise pmd3_exc.PyMobileDevice3Exception("refused: InstallationLookupFailed")
        return self._open(bundle_id, self.documents)

    def _open(self, label, files):
        self.opened.append(label)
        afc = FakeAfc(files)
        self.afcs.append(afc)
        return afc

    async def close_afc(self, afc):
        afc.closed = True

    async def stat(self, afc, path):
        afc._check(path)
        data = afc.files[path]
        return {
            "st_ifmt": "S_IFDIR" if data is None else "S_IFREG",
            "st_size": str(len(data or b"")),
            "st_mtime": self.times.get(path, "0"),
        }


@pytest.fixture
def api():
    return FakeApi()


@pytest.fixture
def notices():
    return []


@pytest.fixture
def backend(api, notices):
    backend = Pmd3Backend(api=api, notify=notices.append)
    yield backend
    backend.close()


def test_lists_devices_and_reuses_the_connection(backend, api):
    assert backend.list_devices() == [DeviceInfo(UDID, "Test iPhone", "iPhone", "iPhone16,1", "27.2")]
    backend.list_devices()
    assert api.opened == ["lockdown"]


def test_only_file_sharing_apps_are_listed(backend):
    assert backend.list_apps(UDID) == [AppInfo("com.shared", "Shared"), AppInfo("com.noname", "com.noname")]


def test_media_listing_and_odd_timestamps(backend):
    entries = {e.name: e for e in backend.listdir(UDID, MEDIA_ROOT, "/DCIM")}
    assert entries["a.jpg"].size == 10
    assert entries["a.jpg"].mtime == 1_700_000_000
    assert entries["odd-date.bin"].mtime is None  # unrepresentable date is dropped, the file is kept
    assert backend.listdir(UDID, MEDIA_ROOT, "/")[0].is_dir


def test_app_paths_are_inside_documents(backend, api):
    assert {e.name for e in backend.listdir(UDID, "com.shared", "/")} == {"note.txt", "Sub"}
    assert backend.stat(UDID, "com.shared", "/Sub").is_dir
    assert api.opened == ["lockdown", "com.shared"]


def test_sessions_are_reused_per_root(backend, api):
    backend.listdir(UDID, MEDIA_ROOT, "/")
    backend.listdir(UDID, MEDIA_ROOT, "/DCIM")
    backend.listdir(UDID, "com.shared", "/")
    assert api.opened == ["lockdown", "media", "com.shared"]


def test_positional_reads_and_close(backend, api):
    reader = backend.open_read(UDID, MEDIA_ROOT, "/DCIM/a.jpg")
    assert reader.read_at(2, 3) == b"234"
    assert reader.read_at(10, 3) == b""
    reader.close()
    reader.close()
    assert api.afcs[0].handles == {}


def test_missing_path_is_not_found(backend):
    with pytest.raises(NotFound):
        backend.listdir(UDID, MEDIA_ROOT, "/nope")
    with pytest.raises(NotFound):
        backend.open_read(UDID, MEDIA_ROOT, "/nope.txt")


def test_app_that_refuses_sharing_is_not_found(backend):
    with pytest.raises(NotFound):
        backend.listdir(UDID, "com.private", "/")


def test_device_refusal_does_not_drop_the_connection(backend, api):
    backend.listdir(UDID, MEDIA_ROOT, "/")
    api.afcs[0].fail = pmd3_exc.AfcException("permission denied", 10, "/x")
    with pytest.raises(BackendError) as info:
        backend.listdir(UDID, MEDIA_ROOT, "/")
    assert not isinstance(info.value, DeviceGone)
    api.afcs[0].fail = OSError(22, "Invalid argument")
    with pytest.raises(BackendError) as info:
        backend.listdir(UDID, MEDIA_ROOT, "/")
    assert not isinstance(info.value, DeviceGone)
    assert not api.afcs[0].closed


def test_dead_connection_raises_device_gone_then_reconnects(backend, api):
    backend.listdir(UDID, MEDIA_ROOT, "/")
    reader = backend.open_read(UDID, MEDIA_ROOT, "/DCIM/a.jpg")
    api.afcs[0].fail = pmd3_exc.ConnectionTerminatedError()
    with pytest.raises(DeviceGone):
        reader.read_at(0, 4)
    assert api.afcs[0].closed
    assert api.closed_lockdowns == 1
    # The next call opens a fresh connection.
    assert backend.listdir(UDID, MEDIA_ROOT, "/")
    assert api.opened == ["lockdown", "media", "lockdown", "media"]


def test_unplugged_device_disappears_and_returns(backend, api):
    backend.listdir(UDID, MEDIA_ROOT, "/")
    api.present = []
    assert backend.list_devices() == []
    assert api.afcs[0].closed
    with pytest.raises(DeviceGone):
        backend.listdir(UDID, MEDIA_ROOT, "/")
    api.present = [UDID]
    api.lockdown_error = None
    assert len(backend.list_devices()) == 1


def test_untrusted_device_is_skipped_with_one_notice(backend, api, notices):
    api.lockdown_error = pmd3_exc.PairingDialogResponsePendingError()
    assert backend.list_devices() == []
    assert backend.list_devices() == []
    assert len(notices) == 1 and "Trust" in notices[0]
    api.lockdown_error = None
    assert len(backend.list_devices()) == 1
    assert "now connected" in notices[-1]


def test_usbmuxd_unreachable_is_reported_plainly(backend, api):
    async def boom():
        raise ConnectionRefusedError(10061, "refused")

    api.udids = boom
    with pytest.raises(BackendError, match="Apple Mobile Device Service"):
        backend.list_devices()


# -- concurrency and failure handling (found in review) ----------------------


def in_threads(count, call):
    errors = []

    def run():
        try:
            call()
        except Exception as e:  # collected for the assertion below
            errors.append(e)

    threads = [threading.Thread(target=run) for _ in range(count)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return errors


def test_concurrent_first_requests_open_one_connection(backend, api):
    api.delay = 0.05
    errors = in_threads(8, lambda: backend.listdir(UDID, MEDIA_ROOT, "/"))
    assert errors == []
    assert api.opened == ["lockdown", "media"]
    backend.close()
    assert api.closed_lockdowns == 1 and all(afc.closed for afc in api.afcs)


def test_late_failure_on_an_old_connection_does_not_close_the_new_one(backend, api):
    reader = backend.open_read(UDID, MEDIA_ROOT, "/DCIM/a.jpg")
    api.afcs[0].fail = pmd3_exc.ConnectionTerminatedError()
    with pytest.raises(DeviceGone):
        backend.listdir(UDID, MEDIA_ROOT, "/")
    assert backend.listdir(UDID, MEDIA_ROOT, "/")  # reconnected
    with pytest.raises(DeviceGone):
        reader.read_at(0, 4)  # the old reader fails late
    reader.close()
    assert not api.afcs[1].closed
    assert api.closed_lockdowns == 1
    assert backend.listdir(UDID, MEDIA_ROOT, "/")
    assert api.opened == ["lockdown", "media", "lockdown", "media"]


def test_stalled_device_is_dropped_after_the_timeout(backend, api):
    backend.listdir(UDID, MEDIA_ROOT, "/")
    backend.op_timeout = 0.2
    api.afcs[0].hang = True
    started = time.monotonic()
    with pytest.raises(DeviceGone):
        backend.listdir(UDID, MEDIA_ROOT, "/")
    assert time.monotonic() - started < 5
    assert api.afcs[0].closed
    assert backend.listdir(UDID, MEDIA_ROOT, "/")  # a fresh connection works
    assert len(api.afcs) == 2


def test_unexpected_library_error_drops_the_connection(backend, api):
    backend.listdir(UDID, MEDIA_ROOT, "/")
    api.afcs[0].fail = AssertionError("reader loop broke")
    with pytest.raises(DeviceGone):
        backend.listdir(UDID, MEDIA_ROOT, "/")
    assert api.afcs[0].closed


def test_failure_while_opening_app_documents_is_not_an_empty_folder(backend, api):
    api.documents_error = pmd3_exc.StartServiceError("com.apple.mobile.house_arrest", "boom")
    with pytest.raises(DeviceGone):
        backend.listdir(UDID, "com.shared", "/")
    api.documents_error = None
    assert backend.listdir(UDID, "com.shared", "/")


def test_drop_while_another_request_is_opening_a_session(backend, api):
    reader = backend.open_read(UDID, MEDIA_ROOT, "/DCIM/a.jpg")
    api.delay = 0.2
    results = []
    opener = threading.Thread(target=lambda: results.extend(in_threads(1, lambda: backend.listdir(UDID, "com.shared", "/"))))
    opener.start()
    time.sleep(0.05)  # the documents session is now being opened
    api.afcs[0].fail = pmd3_exc.ConnectionTerminatedError()
    with pytest.raises(DeviceGone):
        reader.read_at(0, 1)
    opener.join()
    assert all(isinstance(e, DeviceGone) for e in results)
    api.delay = 0
    backend.close()
    assert all(afc.closed for afc in api.afcs)  # nothing leaked


def test_trust_notice_is_repeated_after_a_replug(backend, api, notices):
    api.lockdown_error = pmd3_exc.PairingDialogResponsePendingError()
    backend.list_devices()
    api.present = []
    backend.list_devices()
    api.present = [UDID]
    backend.list_devices()
    assert len([n for n in notices if "Trust" in n]) == 2


def test_calls_after_close_fail_cleanly(api):
    backend = Pmd3Backend(api=api, notify=lambda m: None)
    reader = backend.open_read(UDID, MEDIA_ROOT, "/DCIM/a.jpg")
    backend.close()
    backend.close()
    with pytest.raises(DeviceGone):
        backend.list_devices()
    with pytest.raises(DeviceGone):
        reader.read_at(0, 1)
    reader.close()


# -- writing ------------------------------------------------------------------


def test_write_rename_and_remove(backend, api):
    writer = backend.open_write(UDID, "com.shared", "/new.txt")
    writer.write(b"hel")
    writer.write(b"lo")
    writer.close()
    writer.close()
    files = api.afcs[0].files
    assert files["/Documents/new.txt"] == b"hello"  # app paths stay inside Documents
    assert api.afcs[0].handles == {}
    backend.mkdir(UDID, "com.shared", "/Dir")
    backend.rename(UDID, "com.shared", "/new.txt", "/Dir/renamed.txt")
    assert files["/Documents/Dir/renamed.txt"] == b"hello" and "/Documents/new.txt" not in files
    backend.set_mtime(UDID, "com.shared", "/Dir/renamed.txt", 1_600_000_000)
    assert api.afcs[0].times["/Documents/Dir/renamed.txt"].timestamp() == 1_600_000_000
    backend.remove(UDID, "com.shared", "/Dir")
    assert "/Documents/Dir" not in files and "/Documents/Dir/renamed.txt" not in files
    with pytest.raises(NotFound):
        backend.remove(UDID, "com.shared", "/Dir")


@pytest.mark.parametrize("path", ["/", "", "//"])
def test_the_top_of_a_root_is_never_changed(backend, api, path):
    backend.listdir(UDID, "com.shared", "/")
    before = dict(api.afcs[0].files)
    for change in (
        lambda: backend.remove(UDID, "com.shared", path),
        lambda: backend.rename(UDID, "com.shared", path, "/x"),
        lambda: backend.rename(UDID, "com.shared", "/note.txt", path),
        lambda: backend.open_write(UDID, "com.shared", path),
        lambda: backend.mkdir(UDID, "com.shared", path),
    ):
        with pytest.raises(BackendError):
            change()
    assert api.afcs[0].files == before


def test_device_refusing_a_write_is_an_error_not_a_disconnect(backend, api):
    writer = backend.open_write(UDID, MEDIA_ROOT, "/DCIM/new.bin")
    api.afcs[0].fail = pmd3_exc.AfcException("no space", 28)
    with pytest.raises(BackendError) as info:
        writer.write(b"x")
    assert not isinstance(info.value, DeviceGone)
    api.afcs[0].fail = None
    assert not api.afcs[0].closed
    writer.close()


def test_write_after_disconnect(backend, api):
    writer = backend.open_write(UDID, MEDIA_ROOT, "/DCIM/new.bin")
    api.afcs[0].fail = pmd3_exc.ConnectionTerminatedError()
    with pytest.raises(DeviceGone):
        writer.write(b"x")
    with pytest.raises(DeviceGone):
        writer.close()  # reported: the data was not saved
    writer.close()
