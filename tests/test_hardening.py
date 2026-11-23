"""Regression tests for problems found in review: odd paths, name collisions, caching, HTTP edge cases."""

import threading
import time
from urllib.parse import quote

import pytest
import requests

from iosfb.backend.base import DeviceInfo, NotFound
from iosfb.backend.mock import MockBackend, MockDevice
from iosfb.server import Server
from iosfb.vfs import Vfs

from .conftest import PHONE

IPHONE = "Gavin's iPhone"


class Clock:
    now = 0.0

    def __call__(self):
        return self.now


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def vfs(backend, clock):
    return Vfs(backend, clock=clock)


@pytest.fixture
def server(backend):
    srv = Server(Vfs(backend), port=0, verbose=0)
    srv.start()
    yield srv
    srv.stop()


def device_paths(backend):
    return [c[3] for c in backend.calls if c[0] in ("listdir", "stat", "open_read")]


# -- paths ------------------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    [
        f"/{IPHONE}/Apps/VLC/..\\Library",
        f"/{IPHONE}/Apps/VLC/../Library",
        f"/{IPHONE}/Apps/VLC/./movie.mkv",
        f"/{IPHONE}/Media/DCIM\\100APPLE",
        f"/{IPHONE}/Media/..",
    ],
)
def test_traversal_and_backslash_paths_never_reach_the_device(vfs, backend, path):
    for call in (vfs.stat, vfs.listdir, vfs.open):
        with pytest.raises(NotFound):
            call(path)
    assert all(".." not in p and "\\" not in p for p in device_paths(backend))


def test_encoded_backslash_traversal_over_http_is_404(server, backend):
    url = server.url.rstrip("/") + quote(f"/{IPHONE}/Apps/VLC/") + "..%5CLibrary%5Cx"
    assert requests.get(url, timeout=10).status_code == 404
    assert all(".." not in p for p in device_paths(backend))


def test_names_not_listed_by_the_device_are_never_sent_to_it(vfs, backend):
    with pytest.raises(NotFound):
        vfs.open(f"/{IPHONE}/Media/DCIM/100APPLE/secret.db")
    assert [c for c in backend.calls if c[0] == "open_read"] == []


def test_lookups_ignore_case(vfs):
    path = "/gavin's iphone/media/dcim/100apple/img_0001.jpg"
    assert vfs.stat(path).size == 10
    assert vfs.open(path).read_at(0, 4) == b"jpeg"
    assert [e.name for e in vfs.listdir("/GAVIN'S IPHONE/apps/vlc/SUBS")] == ["movie.srt"]


# -- names Windows cannot tell apart -----------------------------------------


def test_colliding_file_names_resolve_to_the_same_file_everywhere():
    backend = MockBackend(
        [MockDevice(DeviceInfo("U1", "Phone"), media={"d": {"a.": b"thirteen byte", "a": b"1", "A": b"22"}})]
    )
    vfs = Vfs(backend)
    assert [e.name for e in vfs.listdir("/Phone/Media/d")] == ["a"]
    size = vfs.stat("/Phone/Media/d/a").size
    data = vfs.open("/Phone/Media/d/a").read_at(0, 100)
    assert (size, data) == (13, b"thirteen byte")


def test_every_app_keeps_a_distinct_name():
    apps = {
        "com.a": ("Notes", {"a": b""}),
        "com.b": ("Notes", {"b": b""}),
        "com.c": ("Notes (com.a)", {"c": b""}),
        "com.d": ("notes (COM.A)", {"d": b""}),
    }
    vfs = Vfs(MockBackend([MockDevice(DeviceInfo("U1", "Phone"), apps=apps)]))
    names = [e.name for e in vfs.listdir("/Phone/Apps")]
    assert len(names) == 4
    assert len({n.casefold() for n in names}) == 4
    contents = {vfs.listdir(f"/Phone/Apps/{n}")[0].name for n in names}
    assert contents == {"a", "b", "c", "d"}


def test_devices_whose_names_are_only_dots_are_still_reachable():
    backend = MockBackend(
        [MockDevice(DeviceInfo("AAAA1111", "..."), media={"x": {}}), MockDevice(DeviceInfo("BBBB2222", "."), media={"y": {}})]
    )
    vfs = Vfs(backend)
    names = [e.name for e in vfs.listdir("/")]
    assert len(names) == 2 and all(n not in (".", "..") for n in names)
    assert {vfs.listdir(f"/{n}/Media")[0].name for n in names} == {"x", "y"}


# -- cache ------------------------------------------------------------------


def test_slow_listing_is_still_cached(backend, clock):
    vfs = Vfs(backend, cache_ttl=5.0, clock=clock)
    real_listdir = backend.listdir

    def slow_listdir(udid, root, path):
        if path == "/DCIM/100APPLE":
            clock.now += 4.9  # most of the cache lifetime passes while it loads
        return real_listdir(udid, root, path)

    backend.listdir = slow_listdir
    path = f"/{IPHONE}/Media/DCIM/100APPLE"
    vfs.listdir(path)
    clock.now += 4.0  # 8.9 s after the load began, 4 s after it finished
    vfs.stat(f"{path}/IMG_0001.JPG")
    assert len([c for c in backend.calls if c == ("listdir", PHONE, "media", "/DCIM/100APPLE")]) == 1


def test_concurrent_requests_share_one_device_listing(backend):
    vfs = Vfs(backend)
    vfs.listdir(f"/{IPHONE}/Media")  # warm the parents
    real_listdir = backend.listdir

    def slow_listdir(udid, root, path):
        time.sleep(0.1)
        return real_listdir(udid, root, path)

    backend.listdir = slow_listdir
    threads = [threading.Thread(target=vfs.listdir, args=(f"/{IPHONE}/Media/DCIM",)) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len([c for c in backend.calls if c == ("listdir", PHONE, "media", "/DCIM")]) == 1


# -- HTTP -------------------------------------------------------------------


def test_propfind_without_depth_does_not_walk_the_tree(server, backend):
    resp = requests.request("PROPFIND", server.url, timeout=10)
    assert resp.status_code == 207
    assert "100APPLE" not in resp.text and "Media" not in resp.text
    assert [c for c in backend.calls if c[0] == "listdir"] == []
    deep = requests.request("PROPFIND", server.url, headers={"Depth": "infinity"}, timeout=10)
    assert "Media" not in deep.text


def test_file_without_a_date_has_no_etag_but_keeps_a_date(server, backend):
    backend.devices[PHONE].mtime = None
    url = server.url.rstrip("/") + quote(f"/{IPHONE}/Media/DCIM/100APPLE/IMG_0001.JPG")
    head = requests.head(url, timeout=10)
    assert head.status_code == 200
    assert "ETag" not in head.headers
    assert head.headers["Last-Modified"].endswith("GMT")
    listing = requests.request("PROPFIND", url, headers={"Depth": "0"}, timeout=10).text
    assert "getlastmodified" in listing
    # No validator, so a conditional request must return the content, not 304.
    again = requests.get(url, headers={"If-None-Match": '"a-0"'}, timeout=10)
    assert again.status_code == 200 and again.content == b"jpeg-bytes"


def test_head_does_not_open_the_file(server, backend):
    url = server.url.rstrip("/") + quote(f"/{IPHONE}/Media/DCIM/100APPLE/big.mov")
    assert requests.head(url, timeout=10).status_code == 200
    assert [c for c in backend.calls if c[0] == "open_read"] == []


def test_aborted_download_closes_the_file(server, backend):
    url = server.url.rstrip("/") + quote(f"/{IPHONE}/Media/DCIM/100APPLE/big.mov")
    resp = requests.get(url, stream=True, timeout=10)
    next(resp.iter_content(1024))
    resp.close()
    deadline = time.time() + 5
    while backend.open_readers and time.time() < deadline:
        time.sleep(0.05)
    assert backend.open_readers == 0
