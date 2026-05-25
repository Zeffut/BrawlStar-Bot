"""Tests for ControlWorker and AdbController, fully mocked (no real device)."""
from __future__ import annotations

import threading
import time

import pytest

from bsbot.buses import ControlBus
from bsbot.controls.adb import AdbController
from bsbot.controls.inputs import Action, ActionType
from bsbot.workers.control import ButtonLayout, ControlWorker


class FakeAdbDevice:
    """In-memory ADB device — records shell commands."""

    serial = "FAKE-0001"

    def __init__(self):
        self.commands: list[str] = []

    def shell(self, cmd: str) -> str:
        self.commands.append(cmd)
        return ""


@pytest.fixture
def adb() -> AdbController:
    return AdbController(FakeAdbDevice(), screen_width=1080, screen_height=2400)


class TestAdbController:
    def test_tap_format(self, adb):
        adb.tap(123, 456)
        assert adb.device.commands == ["input tap 123 456"]

    def test_swipe_format(self, adb):
        adb.swipe(10, 20, 30, 40, 250)
        assert adb.device.commands == ["input swipe 10 20 30 40 250"]

    def test_floats_coerced(self, adb):
        adb.tap(123.7, 456.2)
        assert adb.device.commands == ["input tap 123 456"]


class TestActionConstructors:
    def test_tap(self):
        a = Action.tap(5, 10)
        assert a.type == ActionType.TAP
        assert (a.x, a.y) == (5, 10)

    def test_joystick(self):
        a = Action.joystick_move(0.5, -0.5)
        assert a.type == ActionType.JOYSTICK_MOVE
        assert (a.dx, a.dy) == (0.5, -0.5)

    def test_aimed_attack(self):
        a = Action.aimed_attack(640, 360)
        assert a.type == ActionType.AIMED_ATTACK
        assert (a.x, a.y) == (640, 360)

    def test_press_button(self):
        a = Action.press_button("super")
        assert a.type == ActionType.PRESS_BUTTON
        assert a.button == "super"


class TestControlWorker:
    def _start(self, bus: ControlBus, adb: AdbController, stop: threading.Event) -> ControlWorker:
        layout = ButtonLayout(
            joystick_center=(200, 1800),
            joystick_radius=100,
            attack_button=(1700, 1850),
            attack_drag_radius=200,
            super_button=(1500, 1700),
            super_drag_radius=200,
            gadget_button=(1850, 1600),
        )
        w = ControlWorker(bus, adb, stop, layout=layout, joystick_tick_ms=20)
        w.start()
        return w

    def _wait_for_command_count(self, adb, n: int, timeout: float = 1.0) -> None:
        start = time.monotonic()
        while len(adb.device.commands) < n and time.monotonic() - start < timeout:
            time.sleep(0.01)

    def test_tap_dispatched(self, adb):
        bus = ControlBus()
        stop = threading.Event()
        w = self._start(bus, adb, stop)
        try:
            bus.put(Action.tap(100, 200))
            self._wait_for_command_count(adb, 1)
            assert "input tap 100 200" in adb.device.commands
        finally:
            stop.set()
            w.join(timeout=1.0)

    def test_press_button_resolves_coords(self, adb):
        bus = ControlBus()
        stop = threading.Event()
        w = self._start(bus, adb, stop)
        try:
            bus.put(Action.press_button("gadget"))
            self._wait_for_command_count(adb, 1)
            assert "input tap 1850 1600" in adb.device.commands
        finally:
            stop.set()
            w.join(timeout=1.0)

    def test_joystick_repeats(self, adb):
        """Joystick should re-fire swipes periodically while held."""
        bus = ControlBus()
        stop = threading.Event()
        w = self._start(bus, adb, stop)
        try:
            bus.put(Action.joystick_move(1.0, 0.0))  # full right
            time.sleep(0.15)  # ~6-7 ticks at 20ms
            stop.set()
            w.join(timeout=1.0)
            swipes = [c for c in adb.device.commands if c.startswith("input swipe")]
            assert len(swipes) >= 3, f"expected multiple swipes, got: {adb.device.commands}"
            # Check the swipe target is to the right of the center.
            for s in swipes:
                parts = s.split()
                x1, x2 = int(parts[2]), int(parts[4])
                assert x2 > x1
        finally:
            stop.set()
            w.join(timeout=1.0)

    def test_joystick_release_stops_ticks(self, adb):
        bus = ControlBus()
        stop = threading.Event()
        w = self._start(bus, adb, stop)
        try:
            bus.put(Action.joystick_move(0.0, 1.0))
            time.sleep(0.1)
            bus.put(Action.joystick_release())
            time.sleep(0.05)
            n_after_release = len(adb.device.commands)
            time.sleep(0.15)
            n_later = len(adb.device.commands)
            assert n_later == n_after_release, "no new commands after release"
        finally:
            stop.set()
            w.join(timeout=1.0)

    def test_aimed_attack_drags_in_direction(self, adb):
        bus = ControlBus()
        stop = threading.Event()
        w = self._start(bus, adb, stop)
        try:
            # Target to the right of screen center → drag from attack button to the right.
            bus.put(Action.aimed_attack(target_x=1000, target_y=1200))  # screen center is 540,1200
            self._wait_for_command_count(adb, 1)
            assert len(adb.device.commands) == 1
            cmd = adb.device.commands[0]
            assert cmd.startswith("input swipe")
            parts = cmd.split()
            x1, y1, x2, y2 = int(parts[2]), int(parts[3]), int(parts[4]), int(parts[5])
            # x1=1700 (attack button), x2 should be to the right of x1 (~1900).
            assert x1 == 1700
            assert x2 > x1
        finally:
            stop.set()
            w.join(timeout=1.0)
