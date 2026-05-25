"""ControlWorker — consume Actions from ControlBus, translate to ADB inputs."""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass

from bsbot.buses import ControlBus
from bsbot.controls.adb import AdbController
from bsbot.controls.inputs import Action, ActionType

logger = logging.getLogger(__name__)


@dataclass
class ButtonLayout:
    """Pixel coordinates of in-game UI elements, in *device* native resolution.

    Calibrated for Mi 9T Pro (2340×1080 landscape) on 2026-05-25.
    Values estimated from a Brawl Ball screenshot; fine-tune by running
    the bot and observing whether movements / shots land correctly.
    """

    # Joystick center (bottom-left of landscape screen), radius in px
    joystick_center: tuple[int, int] = (370, 870)
    joystick_radius: int = 160
    # Attack button (bottom-right). For aimed-attack, we drag from this
    # button toward the target.
    attack_button: tuple[int, int] = (2070, 870)
    attack_drag_radius: int = 250
    # Super button (just left of attack)
    super_button: tuple[int, int] = (1840, 830)
    super_drag_radius: int = 250
    # Gadget button (above attack)
    gadget_button: tuple[int, int] = (2070, 600)


class ControlWorker(threading.Thread):
    """Pull Actions from ControlBus, send to ADB.

    Joystick semantics: holds the last move direction and re-applies it
    periodically as short swipes (Android `input swipe` is not a continuous
    drag). Released on JOYSTICK_RELEASE or when an action of another type
    arrives mid-game.
    """

    def __init__(
        self,
        bus: ControlBus,
        adb: AdbController,
        stop_event: threading.Event,
        layout: ButtonLayout | None = None,
        joystick_tick_ms: int = 80,
    ):
        super().__init__(name="ControlWorker", daemon=True)
        self.bus = bus
        self.adb = adb
        self.stop_event = stop_event
        self.layout = layout or ButtonLayout()
        self.joystick_tick_ms = joystick_tick_ms

        self._joystick_dir: tuple[float, float] | None = None  # (dx, dy) or None
        self._last_joystick_tick = 0.0

    def run(self) -> None:
        logger.info("ControlWorker starting")
        while not self.stop_event.is_set():
            # Tick the joystick if held.
            if self._joystick_dir is not None:
                now = time.monotonic()
                if (now - self._last_joystick_tick) * 1000.0 >= self.joystick_tick_ms:
                    self._apply_joystick_tick()
                    self._last_joystick_tick = now
            try:
                action: Action = self.bus.get(timeout=0.05)
            except Exception:
                continue
            try:
                self._dispatch(action)
            except Exception as exc:
                logger.exception("ControlWorker action failed: %s (%s)", action, exc)
            finally:
                self.bus.task_done()
        logger.info("ControlWorker stopping")

    def _dispatch(self, action: Action) -> None:
        t = action.type
        if t == ActionType.NOOP:
            return
        logger.info("Action dispatch: %s", action)
        if t == ActionType.TAP:
            self.adb.tap(action.x, action.y)
        elif t == ActionType.SWIPE:
            self.adb.swipe(action.x, action.y, action.x2, action.y2, action.duration_ms)
        elif t == ActionType.JOYSTICK_MOVE:
            self._joystick_dir = (action.dx or 0.0, action.dy or 0.0)
            self._apply_joystick_tick()
            self._last_joystick_tick = time.monotonic()
        elif t == ActionType.JOYSTICK_RELEASE:
            self._joystick_dir = None
        elif t == ActionType.PRESS_BUTTON:
            self.adb.tap(*self._button_coords(action.button))
        elif t in (ActionType.AIMED_ATTACK, ActionType.AIMED_SUPER):
            self._aimed(action)
        else:
            logger.warning("Unknown action type: %s", t)

    def _apply_joystick_tick(self) -> None:
        if self._joystick_dir is None:
            return
        dx, dy = self._joystick_dir
        # Clamp + normalize to unit disk.
        mag = max((dx * dx + dy * dy) ** 0.5, 1e-6)
        if mag > 1.0:
            dx /= mag
            dy /= mag
        cx, cy = self.layout.joystick_center
        r = self.layout.joystick_radius
        tx, ty = int(cx + dx * r), int(cy + dy * r)
        # Short swipe with slightly-longer-than-tick duration to feel continuous.
        self.adb.swipe(cx, cy, tx, ty, duration_ms=self.joystick_tick_ms + 20)

    def _aimed(self, action: Action) -> None:
        """Drag from attack/super button toward target world coords."""
        is_super = action.type == ActionType.AIMED_SUPER
        bx, by = self.layout.super_button if is_super else self.layout.attack_button
        r = self.layout.super_drag_radius if is_super else self.layout.attack_drag_radius
        # Compute direction from button to target *on screen*. For Colt the
        # attack is aimed in the same screen direction as the drag.
        # We assume action.x/y are pixel coords on the captured frame, in the
        # same resolution as device. (If frame is downscaled, BrainWorker
        # should rescale before issuing the action.)
        cx, cy = self.adb.screen_width // 2, self.adb.screen_height // 2
        dx = (action.x or cx) - cx
        dy = (action.y or cy) - cy
        mag = max((dx * dx + dy * dy) ** 0.5, 1e-6)
        ux, uy = dx / mag, dy / mag
        tx, ty = int(bx + ux * r), int(by + uy * r)
        self.adb.swipe(bx, by, tx, ty, duration_ms=120)

    def _button_coords(self, button: str | None) -> tuple[int, int]:
        if button == "attack":
            return self.layout.attack_button
        if button == "super":
            return self.layout.super_button
        if button == "gadget":
            return self.layout.gadget_button
        raise ValueError(f"Unknown button: {button}")
