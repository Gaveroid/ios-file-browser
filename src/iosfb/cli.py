"""Command line: ``iosfb devices``, ``iosfb serve``, ``iosfb unmount``."""

from __future__ import annotations

import argparse
import logging
import re
import signal
import subprocess
import sys
import time

from . import __version__
from .backend.base import BackendError, DeviceBackend
from .server import DEFAULT_PORT, Server
from .vfs import Vfs


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="iosfb", description=__doc__)
    parser.add_argument("--version", action="version", version=f"iosfb {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    devices = sub.add_parser("devices", help="list connected devices and what they share")
    devices.add_argument("--demo", action="store_true", help="use a fake device instead of USB")

    serve = sub.add_parser("serve", help="serve connected devices as a WebDAV share on 127.0.0.1")
    serve.add_argument("--port", type=int, default=DEFAULT_PORT, help=f"TCP port (default {DEFAULT_PORT})")
    serve.add_argument("--mount", metavar="LETTER", help="also map the share to this drive letter, e.g. Z:")
    serve.add_argument("--demo", action="store_true", help="serve a fake device instead of USB")
    serve.add_argument(
        "--writable",
        action="store_true",
        help="allow creating, changing, renaming and deleting files on the device (default: read-only)",
    )
    serve.add_argument("-v", "--verbose", action="count", default=0, help="log requests (-vv for more)")

    for command in (devices, serve):
        command.add_argument("--usb-only", action="store_true", help="ignore devices that are only reachable over Wi-Fi")

    unmount = sub.add_parser("unmount", help="remove a drive mapping made with --mount")
    unmount.add_argument("letter", metavar="LETTER")

    args = parser.parse_args(argv)
    for stream in (sys.stdout, sys.stderr):  # device names may not fit the console code page
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="replace")
    logging.basicConfig(level=logging.DEBUG if getattr(args, "verbose", 0) >= 2 else logging.WARNING, format="%(message)s")
    try:
        if args.command == "devices":
            return _devices(args)
        if args.command == "serve":
            return _serve(args)
        return _unmount(_letter(args.letter))
    except BackendError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


def _backend(demo: bool, wifi: bool = True) -> DeviceBackend:
    if demo:
        from .backend.mock import demo_backend

        return demo_backend()
    from .backend.pmd3 import Pmd3Backend

    return Pmd3Backend(notify=lambda message: print(message, flush=True), wifi=wifi)


def _devices(args) -> int:
    backend = _backend(args.demo, not args.usb_only)
    try:
        devices = backend.list_devices()
        if not devices:
            print("No devices found. Connect an iPhone or iPad by USB, unlock it and tap Trust.")
            return 1
        for device in devices:
            print(f"{device.name}  ({device.product_type}, {device.device_class} OS {device.os_version}, {device.udid})")
            apps = sorted(backend.list_apps(device.udid), key=lambda a: a.name.casefold())
            print(f"  Media area, and documents of {len(apps)} apps:")
            for app in apps:
                print(f"    {app.name}  [{app.bundle_id}]")
        return 0
    finally:
        backend.close()


def _serve(args) -> int:
    letter = _letter(args.mount) if args.mount else None
    backend = _backend(args.demo, not args.usb_only)
    server = Server(Vfs(backend, writable=args.writable), port=args.port, verbose=min(args.verbose + 1, 3) if args.verbose else 0)
    try:
        server.start()
    except OSError as exc:
        backend.close()
        print(f"error: cannot listen on 127.0.0.1:{args.port} ({exc}). Try another --port.", file=sys.stderr)
        return 1
    mounted = False
    if hasattr(signal, "SIGBREAK"):  # Ctrl+Break should clean up like Ctrl+C
        signal.signal(signal.SIGBREAK, _interrupt)
    try:
        mode = "READ-WRITE: changes and deletions on the drive happen on the device" if args.writable else "read-only"
        print(f"Serving on {server.url} (this computer only, {mode}).")
        try:
            names = [d.name for d in backend.list_devices()]
        except BackendError as exc:
            names = []
            print(f"warning: {exc}")
        print("Devices: " + (", ".join(names) if names else "none yet; connect one and it will appear."))
        if letter:
            mounted = _mount(letter, server.url)
        else:
            print(f"Map it as a drive with:  net use Z: {server.url}")
        print("Press Ctrl+C to stop.")
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\nStopping.")
    finally:
        if mounted:
            _unmount(letter)
        server.stop()
        backend.close()
    return 0


# net.exe writes in the console (OEM) code page, which can hold bytes the
# default decoder rejects.
_NET_TEXT = {"encoding": "oem" if sys.platform == "win32" else None, "errors": "replace"}


def _interrupt(signum, frame):
    raise KeyboardInterrupt


def _letter(value: str) -> str:
    match = re.fullmatch(r"([A-Za-z]):?\\?", value.strip())
    if not match:
        raise SystemExit(f"error: {value!r} is not a drive letter; use something like Z:")
    return match.group(1).upper() + ":"


def _mount(letter: str, url: str) -> bool:
    result = subprocess.run(["net", "use", letter, url, "/persistent:no"], capture_output=True, **_NET_TEXT)
    if result.returncode == 0:
        print(f"Mapped {letter} to the share.")
        return True
    print(f"warning: could not map {letter}: {(result.stderr or result.stdout).strip()}")
    print(f"You can still open {url} in Explorer or a browser.")
    return False


def _unmount(letter: str) -> int:
    result = subprocess.run(["net", "use", letter, "/delete", "/y"], capture_output=True, **_NET_TEXT)
    if result.returncode == 0:
        print(f"Removed {letter}.")
    else:
        print(f"warning: could not remove {letter}: {(result.stderr or result.stdout).strip()}")
    return result.returncode


if __name__ == "__main__":
    sys.exit(main())
