import pytest

from iosfb.backend.base import DeviceInfo
from iosfb.backend.mock import MockBackend, MockDevice

PHONE = "00008130-AAAA"
PAD = "00008101-BBBB"
TWIN = "00008101-CCCC"

BIG = bytes(range(256)) * 20_000  # 5.12 MB, position-dependent content


@pytest.fixture
def backend() -> MockBackend:
    return MockBackend(
        [
            MockDevice(
                info=DeviceInfo(PHONE, "Gavin's iPhone", "iPhone", "iPhone16,1", "27.2.1"),
                media={
                    "DCIM": {"100APPLE": {"IMG_0001.JPG": b"jpeg-bytes", "big.mov": BIG}},
                    "Downloads": {},
                    "odd": {"a:b?.txt": b"colon", "trailing.": b"dot"},
                },
                apps={
                    "org.videolan.vlc-ios": ("VLC", {"movie.mkv": b"mkv", "Subs": {"movie.srt": b"srt"}}),
                    "com.a.notes": ("Notes", {"a.txt": b"A"}),
                    "com.b.notes": ("Notes", {"b.txt": b"B"}),
                },
            ),
            MockDevice(info=DeviceInfo(PAD, "iPad", "iPad"), media={"Books": {}}),
            MockDevice(info=DeviceInfo(TWIN, "iPad", "iPad"), media={"Music": {}}),
        ]
    )
