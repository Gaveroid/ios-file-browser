"""The real backend: USB-connected devices through pymobiledevice3.

pymobiledevice3 is asyncio-only and its objects must stay on one event loop,
while the WebDAV server is threaded. So this backend owns a private loop on a
background thread and every public method hands a coroutine to it.

Device access needs Apple Mobile Device Support (from iTunes or the "Apple
Devices" app) to be installed; it provides the usbmuxd endpoint used here.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import logging
import posixpath
import threading
from collections.abc import Callable, Coroutine
from datetime import datetime, timezone
from typing import Any, TypeVar

from .base import MEDIA_ROOT, AppInfo, BackendError, DeviceGone, DeviceInfo, Entry, NotAllowed, NotFound

log = logging.getLogger(__name__)

T = TypeVar("T")

OP_TIMEOUT = 60.0  # seconds for one device operation
STAT_CONCURRENCY = 16  # stat requests in flight while listing a folder
DOCUMENTS = "/Documents"
APP_ATTRIBUTES = ["CFBundleIdentifier", "CFBundleDisplayName", "CFBundleName", "UIFileSharingEnabled"]


class Pmd3Api:
    """The few pymobiledevice3 calls this backend makes, isolated so tests can fake them."""

    def __init__(self, wifi: bool = True) -> None:
        from pymobiledevice3 import exceptions

        self.exc = exceptions
        self._wifi = wifi
        self._via: dict[str, str] = {}  # udid -> "USB" or "Network", as last listed

    async def udids(self) -> list[str]:
        """Devices Apple's service can reach: by USB, and over Wi-Fi where the device allows it.

        A device reachable both ways is used over USB.
        """
        from pymobiledevice3 import usbmux

        via: dict[str, str] = {}
        for device in await usbmux.list_devices():
            kind = device.connection_type
            if kind == "USB" or (kind == "Network" and self._wifi and device.serial not in via):
                via[device.serial] = kind
        self._via = via
        return list(via)

    async def open_lockdown(self, udid: str):
        from pymobiledevice3.lockdown import create_using_usbmux

        kind = self._via.get(udid, "USB")
        return await create_using_usbmux(serial=udid, connection_type=kind, autopair=kind == "USB", pair_timeout=0)

    async def close_lockdown(self, lockdown) -> None:
        await lockdown.close()

    def device_info(self, udid: str, lockdown) -> DeviceInfo:
        values = lockdown.all_values
        return DeviceInfo(
            udid=udid,
            name=str(values.get("DeviceName") or udid),
            device_class=str(values.get("DeviceClass") or ""),
            product_type=str(values.get("ProductType") or ""),
            os_version=str(values.get("ProductVersion") or ""),
        )

    async def browse_apps(self, lockdown) -> list[dict]:
        # User apps only: system apps that claim file sharing (Fitness, for
        # one) are refused by the device anyway.
        from pymobiledevice3.services.installation_proxy import InstallationProxyService

        async with InstallationProxyService(lockdown) as proxy:
            return await proxy.browse({"ApplicationType": "User"}, attributes=APP_ATTRIBUTES)

    async def open_media(self, lockdown):
        from pymobiledevice3.services.afc import AfcService

        afc = AfcService(lockdown)
        await afc.connect()
        return afc

    async def open_documents(self, lockdown, bundle_id: str):
        from pymobiledevice3.services.house_arrest import HouseArrestService

        return await HouseArrestService.create(lockdown, bundle_id, documents_only=True)

    async def close_afc(self, afc) -> None:
        await afc.aclose()

    async def stat(self, afc, path: str) -> dict[str, Any]:
        """Return the device's raw attributes for ``path`` (all values are strings).

        ``AfcService.stat`` converts timestamps with ``datetime.fromtimestamp``,
        which raises on Windows for dates it cannot represent, so the raw
        reply is read here and converted leniently by the caller.
        """
        from pymobiledevice3.services import afc as afc_module

        try:
            reply = await afc._do_operation(afc_module.AfcOpcode.GET_FILE_INFO, afc_module.CSTRING.build(path), path)
        except self.exc.AfcException as e:
            if e.status != afc_module.AfcError.READ_ERROR:
                raise
            raise self.exc.AfcFileNotFoundError(f"No such file on the device: {path}", e.status, path) from e
        return afc_module.list_to_dict(reply)


class _Device:
    def __init__(self, info: DeviceInfo, lockdown) -> None:
        self.info = info
        self.lockdown = lockdown
        self.sessions: dict[str, Any] = {}  # root -> AfcService
        self.lock = asyncio.Lock()
        self.alive = True  # False once dropped; its connections must not be used again


class _Reader:
    def __init__(self, backend: Pmd3Backend, device: _Device, afc, handle: int) -> None:
        self._backend = backend
        self._device = device
        self._afc = afc
        self._handle = handle
        self._closed = False

    def read_at(self, offset: int, size: int) -> bytes:
        if self._backend._closed:
            raise DeviceGone("the backend is shut down")

        async def read() -> bytes:
            if not self._device.alive:  # never touch (and so revive) a dropped connection
                raise DeviceGone(self._device.info.name)
            return await self._afc.pread(self._handle, size, offset)

        return self._backend._run(self._backend._translate(self._device, read()))

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._backend._closed:
            return

        async def close() -> None:
            if self._device.alive:
                await _quietly(self._afc.fclose(self._handle))

        try:
            self._backend._run(close())
        except Exception:  # the backend may already be shut down
            log.debug("closing a file handle failed", exc_info=True)


class _Writer:
    def __init__(self, backend: Pmd3Backend, device: _Device, afc, handle: int) -> None:
        self._backend = backend
        self._device = device
        self._afc = afc
        self._handle = handle
        self._closed = False

    def write(self, data: bytes) -> None:
        self._on_device(lambda: self._afc.fwrite(self._handle, bytes(data)))

    def close(self) -> None:
        """Close the file. Unlike a reader's close this reports failure: the data may not be saved."""
        if self._closed:
            return
        self._closed = True
        self._on_device(lambda: self._afc.fclose(self._handle))

    def _on_device(self, call: Callable[[], Coroutine[Any, Any, Any]]) -> None:
        if self._backend._closed:
            raise DeviceGone("the backend is shut down")

        async def run() -> None:
            if not self._device.alive:
                raise DeviceGone(self._device.info.name)
            await call()

        self._backend._run(self._backend._translate(self._device, run()))


class Pmd3Backend:
    def __init__(self, api: Pmd3Api | None = None, notify: Callable[[str], None] | None = None, wifi: bool = True) -> None:
        self._api = api or Pmd3Api(wifi=wifi)
        self._notify = notify or (lambda message: log.warning("%s", message))
        self._devices: dict[str, _Device] = {}
        self._connecting: dict[str, asyncio.Lock] = {}
        self._notices: dict[str, str] = {}  # udid -> last notice shown
        self._closed = False
        self._loop = _new_loop()
        self._thread = threading.Thread(target=self._loop.run_forever, name="iosfb-device", daemon=True)
        self._thread.start()

    # -- DeviceBackend ------------------------------------------------------

    def list_devices(self) -> list[DeviceInfo]:
        return self._run(self._list_devices())

    def list_apps(self, udid: str) -> list[AppInfo]:
        return self._run(self._on_device(udid, self._list_apps))

    def listdir(self, udid: str, root: str, path: str) -> list[Entry]:
        return self._run(self._on_device(udid, lambda device: self._listdir(device, root, path)))

    def stat(self, udid: str, root: str, path: str) -> Entry:
        return self._run(self._on_device(udid, lambda device: self._stat(device, root, path)))

    def open_read(self, udid: str, root: str, path: str) -> _Reader:
        async def open_(device: _Device) -> _Reader:
            afc = await self._session(device, root)
            handle = await afc.fopen(_device_path(root, path), "r")
            return _Reader(self, device, afc, handle)

        return self._run(self._on_device(udid, open_))

    def open_write(self, udid: str, root: str, path: str) -> _Writer:
        async def open_(device: _Device) -> _Writer:
            afc = await self._session(device, root)
            handle = await afc.fopen(_changeable(root, path), "w")
            return _Writer(self, device, afc, handle)

        return self._run(self._on_device(udid, open_))

    def mkdir(self, udid: str, root: str, path: str) -> None:
        self._change(udid, root, lambda afc: afc.makedirs(_changeable(root, path)))

    def remove(self, udid: str, root: str, path: str) -> None:
        self._change(udid, root, lambda afc: afc.rm(_changeable(root, path)))

    def rename(self, udid: str, root: str, source: str, target: str) -> None:
        self._change(udid, root, lambda afc: afc.rename(_changeable(root, source), _changeable(root, target)))

    def set_mtime(self, udid: str, root: str, path: str, mtime: float) -> None:
        when = datetime.fromtimestamp(mtime, timezone.utc)
        self._change(udid, root, lambda afc: afc.set_file_time(_changeable(root, path), when))

    def _change(self, udid: str, root: str, call: Callable[[Any], Coroutine[Any, Any, Any]]) -> None:
        async def change(device: _Device) -> None:
            await call(await self._session(device, root))

        self._run(self._on_device(udid, change))

    def close(self) -> None:
        if self._closed:
            return

        async def shutdown() -> None:
            for device in list(self._devices.values()):
                await self._drop(device)

        try:
            self._run(shutdown(), timeout=15)
        except Exception:
            log.debug("shutdown failed", exc_info=True)
        self._closed = True
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(timeout=5)
        if not self._thread.is_alive():
            self._loop.close()

    # -- loop bridge --------------------------------------------------------

    def _run(self, coro: Coroutine[Any, Any, T], timeout: float | None = None) -> T:
        """Run ``coro`` on the device loop and wait for it.

        Device operations time out inside ``_translate`` (which also drops the
        stuck connection); the timeout here is only a backstop.
        """
        if self._closed:
            coro.close()
            raise DeviceGone("the backend is shut down")
        future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        try:
            return future.result(timeout if timeout is not None else self.op_timeout + 30)
        except concurrent.futures.TimeoutError:
            future.cancel()
            raise DeviceGone("the device did not answer in time") from None

    op_timeout = OP_TIMEOUT

    async def _on_device(self, udid: str, operation: Callable[[_Device], Coroutine[Any, Any, T]]) -> T:
        device = await self._device(udid)
        return await self._translate(device, operation(device))

    async def _translate(self, device: _Device, coro: Coroutine[Any, Any, T]) -> T:
        """Await ``coro`` and turn pymobiledevice3 errors into backend errors.

        A failure that means the connection is unusable drops *that* device
        object, so a late error from an old connection cannot close a newer one.
        """
        exc = self._api.exc
        try:
            return await asyncio.wait_for(coro, self.op_timeout)
        except BackendError:
            raise
        except exc.AfcFileNotFoundError as e:
            raise NotFound(str(e)) from e
        except exc.AfcException as e:  # a refusal from the device, not a dead link
            raise BackendError(f"device refused the request: {e!r}") from e
        except (exc.PasswordRequiredError, exc.NotPairedError) as e:
            await self._drop(device)
            self._notice(device.info.udid, "is locked or not trusted. Unlock it and tap Trust.")
            raise DeviceGone(str(e) or type(e).__name__) from e
        except asyncio.TimeoutError as e:
            await self._drop(device)
            raise DeviceGone("the device did not answer in time") from e
        except (OSError, ValueError) as e:
            if isinstance(e, ConnectionError):
                await self._drop(device)
                raise DeviceGone(str(e) or type(e).__name__) from e
            raise BackendError(f"{type(e).__name__}: {e}") from e
        except Exception as e:
            # Unplugged, a stale connection, or something unexpected inside the
            # library: forget the connection; the next call reconnects.
            await self._drop(device)
            raise DeviceGone(str(e) or type(e).__name__) from e

    # -- devices ------------------------------------------------------------

    async def _list_devices(self) -> list[DeviceInfo]:
        try:
            present = await asyncio.wait_for(self._api.udids(), 20)
        except Exception as e:
            raise BackendError(
                "Cannot reach Apple Mobile Device Service. Install iTunes or the Apple Devices app "
                f"and check the service is running. ({e!r})"
            ) from e
        for udid in set(self._devices) - set(present):
            await self._drop(self._devices[udid])
        for udid in set(self._notices) - set(present):
            del self._notices[udid]  # so a replugged device is announced again
        for udid in set(self._connecting) - set(present):
            if not self._connecting[udid].locked():
                del self._connecting[udid]
        out = []
        for udid in present:
            device = await self._connect(udid)
            if device is not None:
                out.append(device.info)
        return out

    async def _connect(self, udid: str) -> _Device | None:
        """Return the device, pairing if needed; None while it is not usable yet."""
        device = self._devices.get(udid)
        if device is not None:
            return device
        # One connection attempt per device at a time; latecomers reuse its result.
        async with self._connecting.setdefault(udid, asyncio.Lock()):
            device = self._devices.get(udid)
            if device is not None:
                return device
            exc = self._api.exc
            try:
                lockdown = await asyncio.wait_for(self._api.open_lockdown(udid), 20)
            except exc.PairingDialogResponsePendingError:
                self._notice(udid, "is asking for trust. Unlock it and tap Trust.")
            except exc.PasswordRequiredError:
                self._notice(udid, "is locked. Unlock it to continue pairing.")
            except exc.UserDeniedPairingError:
                self._notice(udid, "refused pairing. Replug it and tap Trust to try again.")
            except Exception as e:
                self._notice(udid, f"could not be opened ({e!r}).")
            else:
                device = _Device(self._api.device_info(udid, lockdown), lockdown)
                self._devices[udid] = device
                if self._notices.pop(udid, None):
                    self._notify(f"{device.info.name} is now connected.")
                return device
            return None

    async def _device(self, udid: str) -> _Device:
        device = await self._connect(udid)
        if device is None:
            raise DeviceGone(udid)
        return device

    async def _drop(self, device: _Device) -> None:
        if not device.alive:
            return
        device.alive = False
        if self._devices.get(device.info.udid) is device:
            del self._devices[device.info.udid]
        sessions = list(device.sessions.values())
        device.sessions.clear()
        for afc in sessions:
            await _quietly(self._api.close_afc(afc))
        await _quietly(self._api.close_lockdown(device.lockdown))

    def _notice(self, udid: str, message: str) -> None:
        if self._notices.get(udid) != message:
            self._notices[udid] = message
            self._notify(f"Device {udid[-4:]} {message}")

    # -- sessions and file operations --------------------------------------

    async def _session(self, device: _Device, root: str):
        async with device.lock:
            afc = device.sessions.get(root)
            if afc is None:
                if root == MEDIA_ROOT:
                    afc = await self._api.open_media(device.lockdown)
                else:
                    afc = await self._open_documents(device, root)
                if not device.alive:  # dropped while this was opening
                    await _quietly(self._api.close_afc(afc))
                    raise DeviceGone(device.info.name)
                device.sessions[root] = afc
            return afc

    async def _open_documents(self, device: _Device, bundle_id: str):
        exc = self._api.exc
        try:
            return await self._api.open_documents(device.lockdown, bundle_id)
        except exc.PyMobileDevice3Exception as e:
            # house_arrest reports a refusal ("will not share this app") as the
            # plain base exception; anything more specific is a real failure.
            if type(e) is exc.PyMobileDevice3Exception or isinstance(e, exc.AppNotInstalledError):
                raise NotFound(f"{bundle_id}: {e}") from e
            raise

    async def _list_apps(self, device: _Device) -> list[AppInfo]:
        apps = []
        for app in await self._api.browse_apps(device.lockdown):
            bundle_id = app.get("CFBundleIdentifier")
            if bundle_id and app.get("UIFileSharingEnabled"):
                name = app.get("CFBundleDisplayName") or app.get("CFBundleName") or bundle_id
                apps.append(AppInfo(str(bundle_id), str(name)))
        return apps

    async def _listdir(self, device: _Device, root: str, path: str) -> list[Entry]:
        afc = await self._session(device, root)
        base = _device_path(root, path)
        names = await afc.listdir(base)
        gate = asyncio.Semaphore(STAT_CONCURRENCY)

        async def one(name: str) -> Entry | None:
            async with gate:
                try:
                    return await self._stat_entry(afc, posixpath.join(base, name), name)
                except self._api.exc.AfcException:
                    return None  # vanished or unreadable; leave it out

        return [e for e in await asyncio.gather(*(one(n) for n in names)) if e is not None]

    async def _stat(self, device: _Device, root: str, path: str) -> Entry:
        afc = await self._session(device, root)
        return await self._stat_entry(afc, _device_path(root, path), posixpath.basename(path.rstrip("/")))

    async def _stat_entry(self, afc, device_path: str, name: str) -> Entry:
        info = await self._api.stat(afc, device_path)
        if info.get("st_ifmt") == "S_IFLNK":
            info = await self._api.stat(afc, await afc.resolve_path(device_path))
        return Entry(
            name=name,
            is_dir=info.get("st_ifmt") == "S_IFDIR",
            size=_int(info.get("st_size")) or 0,
            mtime=_seconds(info.get("st_mtime")),
        )


def _int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _seconds(nanoseconds: Any) -> float | None:
    """Device timestamps are nanoseconds since the epoch; ignore nonsense values."""
    value = _int(nanoseconds)
    if value is None or not 0 < value < 4_102_444_800 * 10**9:  # up to the year 2100
        return None
    return value / 10**9


def _device_path(root: str, path: str) -> str:
    path = "/" + path.strip("/")
    if root == MEDIA_ROOT:
        return path
    return DOCUMENTS if path == "/" else DOCUMENTS + path


def _changeable(root: str, path: str) -> str:
    """The device path for a write, refusing the top of a root."""
    if not path.strip("/"):
        raise NotAllowed("the top folder cannot be changed")
    return _device_path(root, path)


def _new_loop() -> asyncio.AbstractEventLoop:
    # pymobiledevice3 uses the selector loop on Windows; the proactor loop
    # breaks some of its socket paths.
    return asyncio.SelectorEventLoop()


async def _quietly(coro: Coroutine[Any, Any, Any]) -> None:
    try:
        await asyncio.wait_for(coro, 5)
    except Exception:
        log.debug("cleanup failed", exc_info=True)
