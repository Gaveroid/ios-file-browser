import pytest

from iosfb.backend.base import DeviceGone, NotFound
from iosfb.vfs import Vfs, sanitize

from .conftest import BIG, PHONE

IPHONE = "Gavin's iPhone"


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def vfs(backend, clock):
    return Vfs(backend, cache_ttl=5.0, clock=clock)


def names(entries):
    return sorted(e.name for e in entries)


def test_sanitize():
    assert sanitize('a:b?"c"') == "a：b？＂c＂"
    assert sanitize("trailing. ") == "trailing"
    assert sanitize("...") == "_"
    assert sanitize("tab\there") == "tab_here"


def test_root_lists_devices_with_duplicates_suffixed(vfs):
    assert names(vfs.listdir("/")) == [IPHONE, "iPad (BBBB)", "iPad (CCCC)"]


def test_device_has_apps_and_media(vfs):
    assert names(vfs.listdir(f"/{IPHONE}")) == ["Apps", "Media"]
    assert vfs.stat(f"/{IPHONE}/Apps").is_dir
    assert vfs.stat(f"/{IPHONE}/Media").is_dir


def test_apps_listing_disambiguates_same_name(vfs):
    assert names(vfs.listdir(f"/{IPHONE}/Apps")) == ["Notes (com.a.notes)", "Notes (com.b.notes)", "VLC"]
    assert names(vfs.listdir(f"/{IPHONE}/Apps/Notes (com.b.notes)")) == ["b.txt"]


def test_media_and_app_paths(vfs):
    assert names(vfs.listdir(f"/{IPHONE}/Media")) == ["DCIM", "Downloads", "odd"]
    assert names(vfs.listdir(f"/{IPHONE}/Apps/VLC/Subs")) == ["movie.srt"]
    entry = vfs.stat(f"/{IPHONE}/Media/DCIM/100APPLE/IMG_0001.JPG")
    assert (entry.is_dir, entry.size) == (False, 10)
    assert vfs.stat(f"/{IPHONE}/Apps/VLC").is_dir


def test_open_and_read(vfs):
    reader = vfs.open(f"/{IPHONE}/Media/DCIM/100APPLE/big.mov")
    assert reader.read_at(4096, 100) == BIG[4096:4196]
    reader.close()


@pytest.mark.parametrize(
    "path",
    [
        "/Nobody",
        f"/{IPHONE}/Other",
        f"/{IPHONE}/Apps/Missing",
        f"/{IPHONE}/Media/DCIM/nope.jpg",
        f"/{IPHONE}/Apps/VLC/nope/deeper",
    ],
)
def test_missing_paths(vfs, path):
    with pytest.raises(NotFound):
        vfs.stat(path)


def test_cannot_open_directories_or_virtual_nodes(vfs):
    for path in ["/", f"/{IPHONE}", f"/{IPHONE}/Media", f"/{IPHONE}/Apps/VLC", f"/{IPHONE}/Media/DCIM"]:
        with pytest.raises(NotFound):
            vfs.open(path)


def test_windows_illegal_names_are_shown_safely_and_resolve_back(vfs):
    assert names(vfs.listdir(f"/{IPHONE}/Media/odd")) == ["a：b？.txt", "trailing"]
    assert vfs.stat(f"/{IPHONE}/Media/odd/a：b？.txt").size == 5
    assert vfs.open(f"/{IPHONE}/Media/odd/a：b？.txt").read_at(0, 10) == b"colon"
    assert vfs.open(f"/{IPHONE}/Media/odd/trailing").read_at(0, 10) == b"dot"


def test_listings_are_cached_until_ttl(vfs, backend, clock):
    path = f"/{IPHONE}/Media/DCIM"
    vfs.listdir(path)
    vfs.listdir(path)
    vfs.stat(f"{path}/100APPLE")
    def listings():
        return len([c for c in backend.calls if c == ("listdir", PHONE, "media", "/DCIM")])

    assert listings() == 1
    clock.now = 6.0
    vfs.listdir(path)
    assert listings() == 2


def test_cache_is_bounded(backend, clock):
    vfs = Vfs(backend, cache_size=2, clock=clock)
    vfs.listdir(f"/{IPHONE}/Media")
    vfs.listdir(f"/{IPHONE}/Media/DCIM")
    vfs.listdir(f"/{IPHONE}/Apps/VLC")
    assert len(vfs._cache) == 2


def test_unplug_clears_cache_and_device_disappears(vfs, backend):
    vfs.listdir(f"/{IPHONE}/Media")
    backend.unplug(PHONE)
    with pytest.raises(DeviceGone):
        vfs.listdir(f"/{IPHONE}/Media/DCIM")
    assert IPHONE not in names(vfs.listdir("/"))
    with pytest.raises(NotFound):
        vfs.listdir(f"/{IPHONE}/Media")
    backend.replug(PHONE)
    vfs.invalidate()
    assert names(vfs.listdir(f"/{IPHONE}/Media")) == ["DCIM", "Downloads", "odd"]
