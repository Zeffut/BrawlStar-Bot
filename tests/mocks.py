"""Test doubles for bot components — used to build integration tests
without a real device."""
from __future__ import annotations

import threading
from collections import deque
from dataclasses import dataclass, field

import numpy as np

from bsbot.buses import ControlBus, LatestSlot
from bsbot.controls.adb import AdbController
from bsbot.controls.inputs import Action


class FakeAdbDevice:
    """Records every `shell` invocation, returns empty string."""

    serial = "FAKE-TEST"

    def __init__(self):
        self.commands: list[str] = []

    def shell(self, cmd: str) -> str:
        self.commands.append(cmd)
        return ""


def make_fake_adb(width: int = 2340, height: int = 1080) -> AdbController:
    return AdbController(FakeAdbDevice(), screen_width=width, screen_height=height)


@dataclass
class ScriptedFrameSource:
    """Replay a sequence of frames into a LatestSlot, on a worker thread.

    Each entry is (frame, hold_s). The thread pushes frame, sleeps hold_s,
    moves to next, until exhausted or stop_event is set.

    Use to drive VisionWorker through known states in tests.
    """

    frame_slot: LatestSlot[np.ndarray]
    stop_event: threading.Event
    sequence: list[tuple[np.ndarray, float]]
    loop: bool = False
    _thread: threading.Thread | None = field(default=None, init=False)

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def join(self, timeout: float = 5.0) -> None:
        if self._thread:
            self._thread.join(timeout=timeout)

    def _run(self) -> None:
        import time
        while not self.stop_event.is_set():
            for frame, hold in self.sequence:
                if self.stop_event.is_set():
                    return
                self.frame_slot.set(frame)
                time.sleep(hold)
            if not self.loop:
                return


def drain_actions(bus: ControlBus, timeout_s: float = 1.0) -> list[Action]:
    """Pop all queued actions from a ControlBus within timeout."""
    import time
    actions: list[Action] = []
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            actions.append(bus.get(block=False))
        except Exception:
            time.sleep(0.02)
            if not actions and time.monotonic() < deadline:
                continue
            break
    return actions


def solid_frame(width: int, height: int, color: tuple[int, int, int] = (50, 50, 50)) -> np.ndarray:
    """Generate a solid-color BGR frame."""
    return np.full((height, width, 3), color, dtype=np.uint8)
