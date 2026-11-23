"""The HTTP server. It only ever listens on the loopback interface."""

from __future__ import annotations

import threading

from cheroot import wsgi
from wsgidav.wsgidav_app import WsgiDAVApp

from .dav import VfsProvider
from .vfs import Vfs

LOOPBACK = "127.0.0.1"
DEFAULT_PORT = 8765
_ALLOWED_HOSTS = {"127.0.0.1", "localhost"}
_READ_METHODS = {"GET", "HEAD", "OPTIONS", "PROPFIND"}


class _LocalOnly:
    """Reject requests whose Host header is not a loopback name.

    Binding to 127.0.0.1 keeps other machines out. This also stops a web page
    in the user's browser from reaching the share through DNS rebinding.
    """

    def __init__(self, app) -> None:
        self._app = app

    def __call__(self, environ, start_response):
        host = environ.get("HTTP_HOST", "")
        name = host.rsplit(":", 1)[0] if ":" in host else host
        if name.lower() not in _ALLOWED_HOSTS:
            return self._forbidden(start_response)
        if environ.get("REQUEST_METHOD") == "PROPFIND" and environ.get("HTTP_DEPTH", "infinity").lower() not in ("0", "1"):
            # "infinity" (also the default when the header is missing) would
            # list every folder of every device in one request.
            environ["HTTP_DEPTH"] = "1"
        if environ.get("REQUEST_METHOD") not in _READ_METHODS and environ.get("HTTP_ORIGIN"):
            # Only browsers send Origin. No web page may change files on the device.
            return self._forbidden(start_response)
        return self._app(environ, start_response)

    @staticmethod
    def _forbidden(start_response):
        start_response("403 Forbidden", [("Content-Type", "text/plain"), ("Content-Length", "10")])
        return [b"Forbidden\n"]


def make_app(vfs: Vfs, verbose: int = 1):
    config = {
        "host": LOOPBACK,
        "provider_mapping": {"/": VfsProvider(vfs)},
        # Anonymous: Windows refuses Basic auth over plain HTTP by default, and
        # the socket is loopback-only.
        "simple_dc": {"user_mapping": {"*": True}},
        "http_authenticator": {"accept_basic": False, "accept_digest": False, "default_to_digest": False},
        "lock_storage": True,
        "property_manager": None,
        # Windows follows every upload with a PROPPATCH of the file's dates;
        # this makes WsgiDAV accept it and hand the modified date to the provider.
        "hotfixes": {"emulate_win32_lastmod": True},
        "block_size": 256 * 1024,
        "verbose": verbose,
        "logging": {"enable": verbose >= 3},
        "dir_browser": {"enable": True, "show_user": False, "response_trailer": False},
    }
    return _LocalOnly(WsgiDAVApp(config))


class Server:
    def __init__(self, vfs: Vfs, port: int = DEFAULT_PORT, verbose: int = 1) -> None:
        self._server = wsgi.Server((LOOPBACK, port), make_app(vfs, verbose), numthreads=8, server_name="iosfb")
        self._server.shutdown_timeout = 1
        self._thread: threading.Thread | None = None

    @property
    def port(self) -> int:
        return self._server.bind_addr[1]

    @property
    def url(self) -> str:
        return f"http://{LOOPBACK}:{self.port}/"

    def start(self) -> None:
        """Bind and serve on a background thread. Raises OSError if the port is taken."""
        self._server.prepare()
        self._thread = threading.Thread(target=self._server.serve, name="iosfb-http", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._server.stop()
        if self._thread is not None:
            self._thread.join(timeout=5)
