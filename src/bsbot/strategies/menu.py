"""MenuStrategy — handles non-match states (lobby, end, popup, disconnect).

Strategy v1: dumb but safe. Uses pre-calibrated coordinates (from config) for
each menu button. As long as those coordinates are accurate, this is enough
to enter and exit matches in a loop.
"""
from __future__ import annotations

import time
from dataclasses import dataclass

from bsbot.controls.inputs import Action
from bsbot.strategies.base import Strategy
from bsbot.vision.postprocess import GameState


@dataclass
class MenuCoords:
    """Native-device coordinates of menu buttons.

    Calibrated during initial setup with `tools/capture_template.py` companion.
    These defaults are guesses for a 1080x2400 landscape phone; override via
    config.toml `[menu]` section.
    """

    play_button: tuple[int, int] = (1700, 1100)
    continue_button: tuple[int, int] = (1700, 1900)
    popup_close: tuple[int, int] = (1850, 200)
    reconnect_button: tuple[int, int] = (960, 1200)
    # Generic safe-tap when stuck — center of screen.
    fallback: tuple[int, int] = (960, 540)


class MenuStrategy(Strategy):
    name = "menu"

    def __init__(self, coords: MenuCoords | None = None, action_cooldown_s: float = 1.5):
        self.coords = coords or MenuCoords()
        self.action_cooldown_s = action_cooldown_s
        self._last_action_at = 0.0
        self._last_state: str | None = None

    def decide(self, gs: GameState) -> Action | None:
        # Throttle: avoid spamming the same button if the state hasn't changed.
        now = time.monotonic()
        if (
            gs.state == self._last_state
            and (now - self._last_action_at) < self.action_cooldown_s
        ):
            return None

        action: Action | None = None
        if gs.state == "lobby":
            action = Action.tap(*self.coords.play_button)
        elif gs.state == "end":
            action = Action.tap(*self.coords.continue_button)
        elif gs.state == "popup":
            action = Action.tap(*self.coords.popup_close)
        elif gs.state == "disconnect":
            action = Action.tap(*self.coords.reconnect_button)
        elif gs.state == "starting":
            # Loading screen — just wait.
            action = None
        elif gs.state == "unknown":
            # Don't tap blindly when we don't know what's on screen.
            action = None

        if action is not None:
            self._last_action_at = now
            self._last_state = gs.state
        return action
