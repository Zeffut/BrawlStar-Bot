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

    Calibrated for BlueStacks (2560×1440 landscape) on 2026-05-25.
    Scaled from Mi 9T Pro coords (x*1.094, y*1.333). Fine-tune by
    observing the live overlay.
    """

    # Joystick center (bottom-left of landscape screen), radius in px
    joystick_center: tuple[int, int] = (405, 1160)
    joystick_radius: int = 215
    # Attack button (bottom-right). For aimed-attack, we drag from this
    # button toward the target.
    attack_button: tuple[int, int] = (2265, 1160)
    attack_drag_radius: int = 335
    # Super button (just left of attack)
    super_button: tuple[int, int] = (2015, 1105)
    super_drag_radius: int = 335
    # Gadget button (above attack)
    gadget_button: tuple[int, int] = (2265, 800)


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
        landscape_w: int = 2560,
        landscape_h: int = 1440,
        capture_worker=None,
    ):
        super().__init__(name="ControlWorker", daemon=True)
        self.bus = bus
        self.adb = adb
        self.stop_event = stop_event
        self.layout = layout or ButtonLayout()
        self.joystick_tick_ms = joystick_tick_ms
        # Landscape frame dimensions used for aim direction computation
        # (independent of `adb.screen_width/height` which return portrait).
        self.landscape_w = landscape_w
        self.landscape_h = landscape_h
        # Optional: reference to CaptureWorker — if provided, ControlWorker
        # can use its scrcpy.Client.control for continuous touch (smoother
        # joystick than ADB `input swipe`).
        self.capture_worker = capture_worker

        self._joystick_dir: tuple[float, float] | None = None  # (dx, dy) or None
        self._last_joystick_tick = 0.0
        self._joystick_finger_down = False
        self._joystick_last_pos: tuple[int, int] | None = None

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
            client = getattr(self.capture_worker, "client", None) if self.capture_worker else None
            if client is not None and self._joystick_finger_down and self._joystick_last_pos:
                try:
                    import scrcpy as _scrcpy
                    x, y = self._joystick_last_pos
                    client.control.touch(x, y, _scrcpy.ACTION_UP)
                except Exception:
                    pass
            self._joystick_finger_down = False
            self._joystick_last_pos = None
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

        # Prefer scrcpy continuous touch (no gap between events) if available.
        client = getattr(self.capture_worker, "client", None) if self.capture_worker else None
        if client is not None and getattr(client, "control", None) is not None:
            try:
                import scrcpy as _scrcpy
                if not self._joystick_finger_down:
                    client.control.touch(cx, cy, _scrcpy.ACTION_DOWN)
                    self._joystick_finger_down = True
                client.control.touch(tx, ty, _scrcpy.ACTION_MOVE)
                self._joystick_last_pos = (tx, ty)
                return
            except Exception as exc:
                logger.debug("scrcpy touch failed, falling back to ADB: %s", exc)

        # Fallback: ADB swipe (less smooth).
        self.adb.swipe(cx, cy, tx, ty, duration_ms=self.joystick_tick_ms + 20)

    def _aimed(self, action: Action) -> None:
        """Drag from attack/super button toward target world coords.

        `action.x/y` are pixel coords in the captured frame (landscape
        orientation, e.g. 2340x1080 for Mi 9T Pro). The drag direction is
        computed FROM our position (assumed at landscape screen center,
        because the camera follows the player) TO the target.

        Note: `adb.screen_width/screen_height` come from `wm size` which
        returns PORTRAIT dimensions (1080x2340 even when device is in
        landscape). We use the layout's landscape dimensions instead.
        """
        is_super = action.type == ActionType.AIMED_SUPER
        bx, by = self.layout.super_button if is_super else self.layout.attack_button
        r = self.layout.super_drag_radius if is_super else self.layout.attack_drag_radius
        # Landscape center — same heuristic the strategy uses for my_pos.
        # Hard-coded for now since the device is always in landscape.
        # If we ever support different aspect ratios, derive from the frame.
        cx, cy = self.landscape_w // 2, int(self.landscape_h * 0.55)
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
