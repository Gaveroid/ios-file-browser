"""Write support: off by default, and safe when on."""

from urllib.parse import quote

import pytest
import requests

from iosfb.backend.base import MEDIA_ROOT, BackendError, DeviceGone, NotAllowed, NotFound
from iosfb.server import Server
from iosfb.vfs import PART_PREFIX, Vfs

from .conftest import BIG, PAD, PHONE

IPHONE = "Gavin's iPhone"
VLC = "org.videolan.vlc-ios"


@pytest.fixture
def vfs(backend):
    return Vfs(backend, writable=True)


@pytest.fixture
def server(vfs):
    srv = Server(vfs, port=0, verbose=0)
    srv.start()
    yield srv
    srv.stop()


def url(server, path):
    return server.url.rstrip("/") + quote(path)


def media(backend):
    return backend.devices[PHONE].media


def vlc(backend):
    return backend.devices[PHONE].apps[VLC][1]


def leftovers(tree):
    found = [name for name in tree if name.startswith(PART_PREFIX)]
    for child in tree.values():
        if isinstance(child, dict):
            found += leftovers(child)
    return found


def write(vfs, path, data):
    upload = vfs.create(path)
    upload.write(data)
    upload.close()


# -- the tree -----------------------------------------------------------------


def test_read_only_tree_refuses_every_change(backend):
    vfs = Vfs(backend)
    path = f"/{IPHONE}/Media/DCIM/100APPLE/IMG_0001.JPG"
    for change in (
        lambda: vfs.create(path),
        lambda: vfs.mkdir(f"/{IPHONE}/Media/new"),
        lambda: vfs.delete(path),
        lambda: vfs.move(path, f"/{IPHONE}/Media/x.jpg"),
        lambda: vfs.copy(path, f"/{IPHONE}/Media/x.jpg"),
        lambda: vfs.set_mtime(path, 1.0),
    ):
        with pytest.raises(NotAllowed):
            change()
    assert [c[0] for c in backend.calls if c[0] in ("open_write", "mkdir", "remove", "rename", "set_mtime")] == []


def test_create_and_replace_a_file(vfs, backend):
    write(vfs, f"/{IPHONE}/Media/Downloads/new.txt", b"hello")
    assert media(backend)["Downloads"] == {"new.txt": b"hello"}
    assert vfs.stat(f"/{IPHONE}/Media/Downloads/new.txt").size == 5  # the cached listing was dropped
    write(vfs, f"/{IPHONE}/Media/Downloads/NEW.TXT", b"again")  # same file to Windows
    assert media(backend)["Downloads"] == {"new.txt": b"again"}
    assert backend.open_writers == 0


def test_write_into_app_documents(vfs, backend):
    write(vfs, f"/{IPHONE}/Apps/VLC/Subs/extra.srt", b"subs")
    assert vlc(backend)["Subs"]["extra.srt"] == b"subs"


def test_failed_upload_keeps_the_old_file(vfs, backend):
    path = f"/{IPHONE}/Media/DCIM/100APPLE/IMG_0001.JPG"
    upload = vfs.create(path)
    upload.write(b"half of the new")
    upload.abort()
    assert media(backend)["DCIM"]["100APPLE"]["IMG_0001.JPG"] == b"jpeg-bytes"
    assert leftovers(media(backend)) == []
    assert backend.open_writers == 0


def test_upload_that_cannot_be_finished_keeps_the_old_file(vfs, backend):
    path = f"/{IPHONE}/Media/DCIM/100APPLE/IMG_0001.JPG"
    upload = vfs.create(path)
    upload.write(b"new")
    real_rename = backend.rename
    backend.rename = lambda *a: (_ for _ in ()).throw(BackendError("refused"))
    with pytest.raises(BackendError):
        upload.close()
    backend.rename = real_rename
    assert media(backend)["DCIM"]["100APPLE"]["IMG_0001.JPG"] == b"jpeg-bytes"
    assert leftovers(media(backend)) == []


def test_unplug_during_upload(vfs, backend):
    upload = vfs.create(f"/{IPHONE}/Media/Downloads/new.bin")
    upload.write(b"x")
    backend.unplug(PHONE)
    with pytest.raises(DeviceGone):
        upload.write(b"y")
    upload.abort()  # must not raise
    assert "new.bin" not in media(backend)["Downloads"]


def test_mkdir_delete_and_rename(vfs, backend):
    vfs.mkdir(f"/{IPHONE}/Media/Downloads/sub")
    write(vfs, f"/{IPHONE}/Media/Downloads/sub/a.txt", b"a")
    vfs.move(f"/{IPHONE}/Media/Downloads/sub/a.txt", f"/{IPHONE}/Media/Downloads/sub/b.txt")
    assert media(backend)["Downloads"] == {"sub": {"b.txt": b"a"}}
    vfs.move(f"/{IPHONE}/Media/Downloads/sub", f"/{IPHONE}/Media/moved")
    assert media(backend)["moved"] == {"b.txt": b"a"}
    vfs.delete(f"/{IPHONE}/Media/moved")
    assert "moved" not in media(backend)
    with pytest.raises(NotFound):
        vfs.delete(f"/{IPHONE}/Media/moved")
    with pytest.raises(NotAllowed):
        vfs.mkdir(f"/{IPHONE}/Media/dcim")  # exists, whatever the case


def test_rename_can_change_only_the_case(vfs, backend):
    vfs.move(f"/{IPHONE}/Apps/VLC/movie.mkv", f"/{IPHONE}/Apps/VLC/Movie.MKV")
    assert "Movie.MKV" in vlc(backend) and "movie.mkv" not in vlc(backend)


def test_move_replaces_a_file_but_never_a_folder(vfs, backend):
    write(vfs, f"/{IPHONE}/Media/Downloads/a.txt", b"new")
    write(vfs, f"/{IPHONE}/Media/Downloads/b.txt", b"old")
    vfs.move(f"/{IPHONE}/Media/Downloads/a.txt", f"/{IPHONE}/Media/Downloads/b.txt")
    assert media(backend)["Downloads"] == {"b.txt": b"new"}
    with pytest.raises(NotAllowed):
        vfs.move(f"/{IPHONE}/Media/Downloads/b.txt", f"/{IPHONE}/Media/DCIM")
    with pytest.raises(NotAllowed):
        vfs.move(f"/{IPHONE}/Media/Downloads", f"/{IPHONE}/Media/DCIM")
    assert media(backend)["Downloads"] == {"b.txt": b"new"}


def test_move_between_apps_and_devices_copies_then_deletes(vfs, backend):
    vfs.move(f"/{IPHONE}/Apps/VLC/Subs", f"/{IPHONE}/Media/Downloads/Subs")
    assert media(backend)["Downloads"]["Subs"] == {"movie.srt": b"srt"}
    assert "Subs" not in vlc(backend)
    vfs.move(f"/{IPHONE}/Media/DCIM/100APPLE/big.mov", "/iPad (BBBB)/Media/Books/big.mov")
    assert backend.devices[PAD].media["Books"]["big.mov"] == BIG
    assert "big.mov" not in media(backend)["DCIM"]["100APPLE"]
    assert backend.open_readers == 0 and backend.open_writers == 0


def test_failed_move_between_apps_keeps_the_source(vfs, backend):
    backend.write_error = BackendError("no space")
    with pytest.raises(BackendError):
        vfs.move(f"/{IPHONE}/Apps/VLC/Subs", f"/{IPHONE}/Media/Downloads/Subs")
    backend.write_error = None
    assert vlc(backend)["Subs"] == {"movie.srt": b"srt"}
    assert media(backend)["Downloads"] == {}
    assert backend.open_readers == 0 and backend.open_writers == 0


def test_copy(vfs, backend):
    vfs.copy(f"/{IPHONE}/Apps/VLC/Subs", f"/{IPHONE}/Apps/VLC/Subs2")
    assert vlc(backend)["Subs2"] == {"movie.srt": b"srt"} == vlc(backend)["Subs"]


@pytest.mark.parametrize(
    "path",
    ["/new", f"/{IPHONE}", f"/{IPHONE}/New", f"/{IPHONE}/Media", f"/{IPHONE}/Apps", f"/{IPHONE}/Apps/VLC", f"/{IPHONE}/Apps/NewApp"],
)
def test_the_layout_itself_cannot_be_changed(vfs, backend, path):
    for change in (vfs.create, vfs.mkdir, vfs.delete, lambda p: vfs.move(p, f"/{IPHONE}/Media/x")):
        with pytest.raises(NotAllowed):
            change(path)
    with pytest.raises(NotAllowed):
        vfs.move(f"/{IPHONE}/Media/Downloads", path)
    assert [c[0] for c in backend.calls if c[0] in ("open_write", "mkdir", "remove", "rename")] == []


@pytest.mark.parametrize("name", ["..", "a\\b", "bad\x01name", PART_PREFIX + "x", "x" * 300])
def test_bad_new_names_are_refused(vfs, backend, name):
    with pytest.raises((NotAllowed, NotFound)):
        vfs.create(f"/{IPHONE}/Media/Downloads/{name}")
    assert [c for c in backend.calls if c[0] == "open_write"] == []


def test_set_mtime(vfs, backend):
    path = f"/{IPHONE}/Media/DCIM/100APPLE/IMG_0001.JPG"
    vfs.set_mtime(path, 1_600_000_000.0)
    assert vfs.stat(path).mtime == 1_600_000_000.0


# -- over HTTP ------------------------------------------------------------------


def test_put_get_round_trip(server, backend):
    target = url(server, f"/{IPHONE}/Media/Downloads/big.bin")
    assert requests.put(target, data=BIG, timeout=30).status_code == 201
    assert media(backend)["Downloads"]["big.bin"] == BIG
    assert requests.get(target, timeout=30).content == BIG
    replaced = requests.put(target, data=b"small", timeout=10)
    assert replaced.status_code == 204
    assert requests.get(target, timeout=10).content == b"small"
    head = requests.head(target, timeout=10)
    assert head.headers["Content-Length"] == "5"
    assert leftovers(media(backend)) == [] and backend.open_writers == 0


def test_put_that_fails_midway_keeps_the_old_content(server, backend):
    target = url(server, f"/{IPHONE}/Media/DCIM/100APPLE/IMG_0001.JPG")

    def body():
        yield b"a" * 300_000
        backend.write_error = BackendError("no space left")
        yield b"b" * 10  # small, so the client is never cut off mid-send by the error reply

    resp = requests.put(target, data=body(), timeout=10)
    backend.write_error = None
    assert resp.status_code == 403
    assert requests.get(target, timeout=10).content == b"jpeg-bytes"
    assert leftovers(media(backend)) == [] and backend.open_writers == 0


def test_mkcol_move_delete(server, backend):
    folder = url(server, f"/{IPHONE}/Apps/VLC/New folder")
    assert requests.request("MKCOL", folder, timeout=10).status_code == 201
    assert requests.request("MKCOL", folder, timeout=10).status_code == 405
    assert requests.put(folder + "/a.txt", data=b"a", timeout=10).status_code == 201
    renamed = url(server, f"/{IPHONE}/Apps/VLC/Renamed")
    assert requests.request("MOVE", folder, headers={"Destination": renamed}, timeout=10).status_code == 204
    assert vlc(backend)["Renamed"] == {"a.txt": b"a"} and "New folder" not in vlc(backend)
    clash = requests.request(
        "MOVE", renamed + "/a.txt", headers={"Destination": url(server, f"/{IPHONE}/Apps/VLC/movie.mkv"), "Overwrite": "F"}, timeout=10
    )
    assert clash.status_code == 412
    assert requests.delete(renamed, timeout=10).status_code == 204
    assert "Renamed" not in vlc(backend)
    assert requests.delete(renamed, timeout=10).status_code == 404


def test_copy_over_http(server, backend):
    source = url(server, f"/{IPHONE}/Apps/VLC/movie.mkv")
    dest = url(server, f"/{IPHONE}/Media/Downloads/movie.mkv")
    assert requests.request("COPY", source, headers={"Destination": dest}, timeout=10).status_code == 204
    assert media(backend)["Downloads"]["movie.mkv"] == b"mkv" == vlc(backend)["movie.mkv"]


@pytest.mark.parametrize("path", [f"/{IPHONE}", f"/{IPHONE}/Media", f"/{IPHONE}/Apps", f"/{IPHONE}/Apps/VLC"])
def test_layout_folders_cannot_be_deleted_or_renamed(server, backend, path):
    assert requests.delete(url(server, path), timeout=10).status_code == 403
    moved = requests.request("MOVE", url(server, path), headers={"Destination": url(server, f"/{IPHONE}/Media/x")}, timeout=10)
    assert moved.status_code == 403
    assert requests.put(url(server, f"/{IPHONE}/new.txt"), data=b"x", timeout=10).status_code == 403
    assert "Downloads" in media(backend) and "movie.mkv" in vlc(backend)


def test_windows_style_upload_sets_the_file_date(server, backend):
    """The sequence the Windows client sends: empty PUT, LOCK, PUT, PROPPATCH of the dates, UNLOCK."""
    target = url(server, f"/{IPHONE}/Media/Downloads/photo.jpg")
    assert requests.put(target, data=b"", timeout=10).status_code == 201
    lock_body = (
        '<?xml version="1.0" encoding="utf-8"?><D:lockinfo xmlns:D="DAV:"><D:lockscope><D:exclusive/></D:lockscope>'
        "<D:locktype><D:write/></D:locktype><D:owner><D:href>user</D:href></D:owner></D:lockinfo>"
    )
    lock = requests.request("LOCK", target, data=lock_body, headers={"Timeout": "Second-3600"}, timeout=10)
    assert lock.status_code == 200
    token = lock.headers["Lock-Token"]
    held = {"If": f"({token})"}
    assert requests.put(target, data=b"x", timeout=10).status_code == 423  # locked for anyone else
    assert requests.put(target, data=b"jpeg", headers=held, timeout=10).status_code == 204
    props = (
        '<?xml version="1.0" encoding="utf-8"?><D:propertyupdate xmlns:D="DAV:" xmlns:Z="urn:schemas-microsoft-com:">'
        "<D:set><D:prop><Z:Win32CreationTime>Mon, 14 Sep 2020 10:00:00 GMT</Z:Win32CreationTime>"
        "<Z:Win32LastAccessTime>Mon, 23 Nov 2026 10:00:00 GMT</Z:Win32LastAccessTime>"
        "<Z:Win32LastModifiedTime>Mon, 14 Sep 2020 10:00:00 GMT</Z:Win32LastModifiedTime>"
        "<Z:Win32FileAttributes>00000020</Z:Win32FileAttributes></D:prop></D:set></D:propertyupdate>"
    )
    patched = requests.request("PROPPATCH", target, data=props, headers=held, timeout=10)
    assert patched.status_code == 207 and "200 OK" in patched.text and "403" not in patched.text
    assert requests.request("UNLOCK", target, headers={"Lock-Token": token}, timeout=10).status_code == 204
    assert media(backend)["Downloads"]["photo.jpg"] == b"jpeg"
    assert backend.mtimes[PHONE, MEDIA_ROOT, "/Downloads/photo.jpg"] == 1_600_077_600


def test_requests_from_web_pages_cannot_change_anything(server, backend):
    target = url(server, f"/{IPHONE}/Media/DCIM/100APPLE/IMG_0001.JPG")
    origin = {"Origin": "https://evil.example"}
    assert requests.delete(target, headers=origin, timeout=10).status_code == 403
    assert requests.put(target, data=b"x", headers=origin, timeout=10).status_code == 403
    assert requests.post(target, data=b"x", headers=origin, timeout=10).status_code == 403
    assert requests.get(target, headers=origin, timeout=10).content == b"jpeg-bytes"
    assert media(backend)["DCIM"]["100APPLE"]["IMG_0001.JPG"] == b"jpeg-bytes"
