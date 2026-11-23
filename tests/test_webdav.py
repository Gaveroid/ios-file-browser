"""HTTP-level tests: a real server on an ephemeral port, driven like a WebDAV client."""

import time
import tracemalloc
import xml.etree.ElementTree as ET
from urllib.parse import quote, unquote, urlparse

import pytest
import requests

from iosfb.backend.base import MEDIA_ROOT
from iosfb.dav import READ_AHEAD, DeviceStream
from iosfb.server import Server
from iosfb.vfs import Vfs

from .conftest import BIG, PHONE

IPHONE = "Gavin's iPhone"
DAV = "{DAV:}"


@pytest.fixture
def server(backend):
    srv = Server(Vfs(backend), port=0, verbose=0)
    srv.start()
    yield srv
    srv.stop()


def readers_closed(backend):
    """The server closes a file just after sending its last byte; allow for that."""
    deadline = time.time() + 5
    while backend.open_readers and time.time() < deadline:
        time.sleep(0.01)
    return backend.open_readers == 0


def url(server, path):
    return server.url.rstrip("/") + quote(path)


def propfind(server, path, depth="1"):
    resp = requests.request("PROPFIND", url(server, path), headers={"Depth": depth}, timeout=10)
    assert resp.status_code == 207, resp.text
    out = {}
    for response in ET.fromstring(resp.content).findall(f"{DAV}response"):
        href = unquote(urlparse(response.find(f"{DAV}href").text).path)
        prop = response.find(f"{DAV}propstat/{DAV}prop")
        out[href.rstrip("/") or "/"] = prop
    return out


def test_listens_on_loopback_only(server):
    assert server.url.startswith("http://127.0.0.1:")
    assert server._server.bind_addr[0] == "127.0.0.1"


def test_options_at_root_advertises_dav(server):
    resp = requests.options(server.url, timeout=10)
    assert resp.status_code == 200
    assert "1" in resp.headers["DAV"]
    assert "PROPFIND" in resp.headers["Allow"]
    assert "PUT" not in resp.headers["Allow"]


def test_propfind_root_lists_devices(server):
    hrefs = propfind(server, "/")
    assert set(hrefs) == {"/", f"/{IPHONE}", "/iPad (BBBB)", "/iPad (CCCC)"}


def test_propfind_depth_0(server):
    hrefs = propfind(server, f"/{IPHONE}/Apps", depth="0")
    assert list(hrefs) == [f"/{IPHONE}/Apps"]
    assert hrefs[f"/{IPHONE}/Apps"].find(f"{DAV}resourcetype/{DAV}collection") is not None


def test_propfind_file_properties(server):
    hrefs = propfind(server, f"/{IPHONE}/Media/DCIM/100APPLE")
    prop = hrefs[f"/{IPHONE}/Media/DCIM/100APPLE/big.mov"]
    assert int(prop.find(f"{DAV}getcontentlength").text) == len(BIG)
    assert prop.find(f"{DAV}getlastmodified").text.endswith("GMT")
    assert prop.find(f"{DAV}resourcetype").find(f"{DAV}collection") is None


def test_propfind_costs_one_listing_per_folder(server, backend):
    propfind(server, f"/{IPHONE}/Media/DCIM/100APPLE")
    assert [c for c in backend.calls if c[0] == "stat"] == []
    assert len([c for c in backend.calls if c == ("listdir", PHONE, MEDIA_ROOT, "/DCIM/100APPLE")]) == 1


def test_get_file(server, backend):
    resp = requests.get(url(server, f"/{IPHONE}/Apps/VLC/Subs/movie.srt"), timeout=10)
    assert resp.status_code == 200
    assert resp.content == b"srt"
    assert readers_closed(backend)


def test_get_large_file_streams_in_bounded_reads(server, backend):
    resp = requests.get(url(server, f"/{IPHONE}/Media/DCIM/100APPLE/big.mov"), timeout=30)
    assert resp.content == BIG
    assert resp.headers["Content-Length"] == str(len(BIG))
    assert len(backend.read_calls) >= len(BIG) // (4 * READ_AHEAD)
    assert max(size for _, size in backend.read_calls) <= 4 * READ_AHEAD
    assert readers_closed(backend)


def test_range_requests(server):
    target = url(server, f"/{IPHONE}/Media/DCIM/100APPLE/big.mov")
    resp = requests.get(target, headers={"Range": "bytes=1000000-1000099"}, timeout=10)
    assert resp.status_code == 206
    assert resp.content == BIG[1_000_000:1_000_100]
    assert resp.headers["Content-Range"] == f"bytes 1000000-1000099/{len(BIG)}"
    tail = requests.get(target, headers={"Range": "bytes=-10"}, timeout=10)
    assert tail.status_code == 206
    assert tail.content == BIG[-10:]


def test_head(server):
    resp = requests.head(url(server, f"/{IPHONE}/Media/DCIM/100APPLE/IMG_0001.JPG"), timeout=10)
    assert resp.status_code == 200
    assert resp.headers["Content-Length"] == "10"
    assert resp.headers["Content-Type"] == "image/jpeg"


def test_missing_is_404(server):
    assert requests.get(url(server, f"/{IPHONE}/Media/nope.txt"), timeout=10).status_code == 404
    assert requests.request("PROPFIND", url(server, "/Nobody"), timeout=10).status_code == 404


def test_sanitized_names_round_trip(server):
    hrefs = propfind(server, f"/{IPHONE}/Media/odd")
    assert f"/{IPHONE}/Media/odd/a：b？.txt" in hrefs
    assert requests.get(url(server, f"/{IPHONE}/Media/odd/a：b？.txt"), timeout=10).content == b"colon"


@pytest.mark.parametrize(
    "method, path",
    [
        ("PUT", f"/{IPHONE}/Media/new.txt"),
        ("PUT", f"/{IPHONE}/Media/DCIM/100APPLE/IMG_0001.JPG"),
        ("DELETE", f"/{IPHONE}/Media/DCIM/100APPLE/IMG_0001.JPG"),
        ("MKCOL", f"/{IPHONE}/Media/newdir"),
        ("MOVE", f"/{IPHONE}/Media/DCIM/100APPLE/IMG_0001.JPG"),
        ("COPY", f"/{IPHONE}/Media/DCIM/100APPLE/IMG_0001.JPG"),
        ("PROPPATCH", f"/{IPHONE}/Media/DCIM/100APPLE/IMG_0001.JPG"),
        ("LOCK", f"/{IPHONE}/Media/DCIM/100APPLE/IMG_0001.JPG"),
    ],
)
def test_writes_are_refused(server, backend, method, path):
    headers = {"Destination": url(server, f"/{IPHONE}/Media/moved.jpg")}
    body = b"data" if method == "PUT" else None
    resp = requests.request(method, url(server, path), headers=headers, data=body, timeout=10)
    assert 400 <= resp.status_code < 500, resp.status_code
    assert backend.devices[PHONE].media["DCIM"]["100APPLE"]["IMG_0001.JPG"] == b"jpeg-bytes"
    assert "new.txt" not in backend.devices[PHONE].media


def test_unplugged_device_gives_clean_errors(server, backend):
    target = url(server, f"/{IPHONE}/Media/DCIM/100APPLE/IMG_0001.JPG")
    assert requests.get(target, timeout=10).status_code == 200
    backend.unplug(PHONE)
    # The listing is still cached, but opening the file reaches the device.
    assert requests.get(target, timeout=10).status_code == 503
    # After the failure the cache is dropped and the device folder is gone.
    assert requests.get(target, timeout=10).status_code == 404
    assert f"/{IPHONE}" not in propfind(server, "/")
    assert requests.get(server.url, timeout=10).status_code == 200


def test_foreign_host_header_is_rejected(server):
    resp = requests.get(server.url, headers={"Host": "evil.example"}, timeout=10)
    assert resp.status_code == 403
    assert requests.get(server.url, headers={"Host": f"localhost:{server.port}"}, timeout=10).status_code == 200


def test_browser_listing_works(server):
    resp = requests.get(url(server, f"/{IPHONE}/Apps/"), timeout=10)
    assert resp.status_code == 200
    assert "VLC" in resp.text


class _FakeReader:
    """A 2 GB file that never exists in memory."""

    size = 2 * 1024**3

    def read_at(self, offset, size):
        return b"\0" * max(0, min(size, self.size - offset))

    def close(self):
        pass


def test_stream_memory_stays_bounded_for_huge_files():
    stream = DeviceStream(_FakeReader(), _FakeReader.size)
    tracemalloc.start()
    total = 0
    while chunk := stream.read(256 * 1024):
        total += len(chunk)
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    assert total == _FakeReader.size
    assert peak < 16 * 1024 * 1024


def test_stream_seek_and_short_reads(backend):
    reader = backend.open_read(PHONE, MEDIA_ROOT, "/DCIM/100APPLE/big.mov")
    stream = DeviceStream(reader, len(BIG))
    assert stream.read(10) == BIG[:10]
    stream.seek(len(BIG) - 5)
    assert stream.read(100) == BIG[-5:]
    assert stream.read(100) == b""
    stream.seek(3_000_000)
    assert stream.read() == BIG[3_000_000:]
    stream.close()
    stream.close()
    assert backend.open_readers == 0
