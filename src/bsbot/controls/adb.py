"""Thin wrapper over adbutils — connects to a device and sends raw inputs.

We use the `input` shell command rather than adbutils' higher-level helpers
because it's the lowest-common-denominator that works on every Android version
and is what every screen-mirroring bot uses.
"""
from __future__ import annotations

import logging
from typing import Protocol

logger = logging.getLogger(__name__)


class Device(Protocol):
    """Minimal interface we use from adbutils.AdbDevice (for mocking in tests)."""

    serial: str

    def shell(self, cmd: str) -> str: ...


class AdbController:
    """Wrap an adb device, expose tap/swipe primitives."""

    def __init__(self, device: Device, screen_width: int, screen_height: int) -> None:
        self.device = device
        self.screen_width = screen_width
        self.screen_height = screen_height

    @classmethod
    def connect(cls, serial: str = "") -> "AdbController":
        """Connect to first device (if serial empty) or specific serial.

        Raises RuntimeError if no device or device unauthorized.
        """
        # Imported here to keep tests importable without adbutils installed.
        import adbutils

        adb = adbutils.AdbClient(host="127.0.0.1", port=5037)
        devices = adb.list()
        if not devices:
            raise RuntimeError("No ADB devices found. Connect a phone via USB.")
        if serial:
            target = next((d for d in devices if d.serial == serial), None)
            if target is None:
                raise RuntimeError(f"ADB device with serial '{serial}' not found.")
        else:
            target = devices[0]
        if target.state != "device":
            raise RuntimeError(
                f"ADB device {target.serial} is in state '{target.state}'. "
                "If 'unauthorized', accept the USB debugging dialog on the phone."
            )
        dev = adb.device(serial=target.serial)
        # Get physical screen size.
        try:
            size_out = dev.shell("wm size").strip()
            # Output looks like "Physical size: 1080x2400" possibly with an
            # override line below.
            parts = [line for line in size_out.splitlines() if "size:" in line.lower()]
            last = parts[-1] if parts else size_out
            wh = last.split(":")[-1].strip()
            w, h = (int(x) for x in wh.split("x"))
        except Exception as exc:
            logger.warning("Could not parse screen size (%s); defaulting to 1080x2400", exc)
            w, h = 1080, 2400
        return cls(dev, w, h)

    def tap(self, x: int, y: int) -> None:
        self.device.shell(f"input tap {int(x)} {int(y)}")

    def swipe(self, x1: int, y1: int, x2: int, y2: int, duration_ms: int = 100) -> None:
        self.device.shell(
            f"input swipe {int(x1)} {int(y1)} {int(x2)} {int(y2)} {int(duration_ms)}"
        )

    def keyevent(self, code: int | str) -> None:
        self.device.shell(f"input keyevent {code}")
