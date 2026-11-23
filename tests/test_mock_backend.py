import pytest

from iosfb.backend.base import MEDIA_ROOT, DeviceGone, NotFound

from .conftest import BIG, PHONE


def names(entries):
    return sorted(e.name for e in entries)


def test_lists_devices_and_apps(backend):
    assert len(backend.list_devices()) == 3
    assert {a.bundle_id for a in backend.list_apps(PHONE)} == {"org.videolan.vlc-ios", "com.a.notes", "com.b.notes"}


def test_listdir_and_stat(backend):
    assert names(backend.listdir(PHONE, MEDIA_ROOT, "/")) == ["DCIM", "Downloads", "odd"]
    entry = backend.stat(PHONE, MEDIA_ROOT, "/DCIM/100APPLE/IMG_0001.JPG")
    assert (entry.name, entry.is_dir, entry.size) == ("IMG_0001.JPG", False, 10)
    assert backend.stat(PHONE, "org.videolan.vlc-ios", "/Subs").is_dir


def test_missing_paths_raise_not_found(backend):
    with pytest.raises(NotFound):
        backend.listdir(PHONE, MEDIA_ROOT, "/nope")
    with pytest.raises(NotFound):
        backend.listdir(PHONE, "com.unknown", "/")
    with pytest.raises(NotFound):
        backend.open_read(PHONE, MEDIA_ROOT, "/DCIM")


def test_positional_reads(backend):
    reader = backend.open_read(PHONE, MEDIA_ROOT, "/DCIM/100APPLE/big.mov")
    assert reader.read_at(1000, 16) == BIG[1000:1016]
    assert reader.read_at(len(BIG), 16) == b""
    reader.close()
    assert backend.open_readers == 0


def test_unplugged_device_raises_device_gone(backend):
    reader = backend.open_read(PHONE, MEDIA_ROOT, "/DCIM/100APPLE/big.mov")
    backend.unplug(PHONE)
    assert len(backend.list_devices()) == 2
    with pytest.raises(DeviceGone):
        backend.listdir(PHONE, MEDIA_ROOT, "/")
    with pytest.raises(DeviceGone):
        reader.read_at(0, 1)
