"""Action types — high-level intents emitted by strategies, consumed by ControlWorker."""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class ActionType(str, Enum):
    NOOP = "noop"
    TAP = "tap"
    SWIPE = "swipe"
    # Joystick is a long swipe maintained from a center point. We model it
    # as a "set direction" intent; ControlWorker translates to repeated swipes.
    JOYSTICK_MOVE = "joystick_move"
    JOYSTICK_RELEASE = "joystick_release"
    # Buttons (attack, super, gadget). Coordinates resolved by ControlWorker
    # from a layout map (loaded from config).
    PRESS_BUTTON = "press_button"
    AIMED_ATTACK = "aimed_attack"   # tap-and-drag from attack button toward target
    AIMED_SUPER = "aimed_super"


@dataclass(frozen=True)
class Action:
    type: ActionType
    # Generic params — only the ones relevant to `type` are set.
    x: int | None = None
    y: int | None = None
    x2: int | None = None
    y2: int | None = None
    duration_ms: int = 100
    # For JOYSTICK_MOVE: unit vector (dx, dy) in [-1.0, 1.0].
    dx: float | None = None
    dy: float | None = None
    # For PRESS_BUTTON / AIMED_*: which button.
    button: str | None = None  # "attack", "super", "gadget"

    @classmethod
    def noop(cls) -> "Action":
        return cls(type=ActionType.NOOP)

    @classmethod
    def tap(cls, x: int, y: int) -> "Action":
        return cls(type=ActionType.TAP, x=x, y=y)

    @classmethod
    def swipe(cls, x1: int, y1: int, x2: int, y2: int, duration_ms: int = 100) -> "Action":
        return cls(type=ActionType.SWIPE, x=x1, y=y1, x2=x2, y2=y2, duration_ms=duration_ms)

    @classmethod
    def joystick_move(cls, dx: float, dy: float) -> "Action":
        return cls(type=ActionType.JOYSTICK_MOVE, dx=dx, dy=dy)

    @classmethod
    def joystick_release(cls) -> "Action":
        return cls(type=ActionType.JOYSTICK_RELEASE)

    @classmethod
    def press_button(cls, button: str) -> "Action":
        return cls(type=ActionType.PRESS_BUTTON, button=button)

    @classmethod
    def aimed_attack(cls, target_x: int, target_y: int) -> "Action":
        return cls(type=ActionType.AIMED_ATTACK, x=target_x, y=target_y)

    @classmethod
    def aimed_super(cls, target_x: int, target_y: int) -> "Action":
        return cls(type=ActionType.AIMED_SUPER, x=target_x, y=target_y)
