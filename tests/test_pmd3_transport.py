"""Which devices are used, and over which link, given what Apple's service lists."""

import asyncio
from types import SimpleNamespace

import pytest
from pymobiledevice3 import lockdown, usbmux

from iosfb.backend.pmd3 import Pmd3Api


def listed(*devices):
    async def list_devices():
        return [SimpleNamespace(serial=serial, connection_type=kind) for serial, kind in devices]

    return list_devices


@pytest.fixture
def opened(monkeypatch):
    calls = []

    async def create_using_usbmux(**kwargs):
        calls.append(kwargs)

    monkeypatch.setattr(lockdown, "create_using_usbmux", create_using_usbmux)
    return calls


def test_usb_is_preferred_and_wifi_only_devices_are_included(monkeypatch, opened):
    monkeypatch.setattr(usbmux, "list_devices", listed(("A", "Network"), ("A", "USB"), ("B", "Network"), ("C", "USB")))
    api = Pmd3Api()
    assert asyncio.run(api.udids()) == ["A", "B", "C"]
    asyncio.run(api.open_lockdown("A"))
    asyncio.run(api.open_lockdown("B"))
    assert [(c["serial"], c["connection_type"], c["autopair"]) for c in opened] == [("A", "USB", True), ("B", "Network", False)]


def test_usb_only(monkeypatch):
    monkeypatch.setattr(usbmux, "list_devices", listed(("A", "USB"), ("A", "Network"), ("B", "Network")))
    assert asyncio.run(Pmd3Api(wifi=False).udids()) == ["A"]
