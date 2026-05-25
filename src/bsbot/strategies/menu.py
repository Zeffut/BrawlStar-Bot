"""MenuStrategy — handles non-match states (lobby, end, popup, disconnect, starting).

Strategy v1: dumb but safe. Uses pre-calibrated coordinates (from config) for
each menu button. As long as those coordinates are accurate, this is enough
to enter and exit matches in a loop.

Special handling for `starting` (the daily Victory star drop, which expects a
real "touch and hold" gesture that Unity ignores when synthesized): if we're
stuck there for too long, request an app restart via a special Action.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass

from bsbot.controls.inputs import Action, ActionType
from bsbot.strategies.base import Strategy
from bsbot.vision.postprocess import GameState

logger = logging.getLogger(__name__)


@dataclass
class MenuCoords:
    """Native-device coordinates of menu buttons.

    Calibrated during initial setup with `tools/capture_template.py` companion.
    These defaults are guesses for a 1080x2400 landscape phone; override via
    config.toml `[menu]` section.
    """

    # Calibrated for Mi 9T Pro (2340x1080 landscape) on 2026-05-25.
    play_button: tuple[int, int] = (2115, 930)
    continue_button: tuple[int, int] = (2130, 1000)  # blue CONTINUER button bottom-right
    # Top-right home icon — works to dismiss reward popups (star drop, coins, brawler unlocked).
    popup_close: tuple[int, int] = (2204, 58)
    # "RECHARGER" link in the AFK-kick popup (Déconnexion pour non-participation).
    reconnect_button: tuple[int, int] = (575, 728)
    # Generic safe-tap when stuck — center of screen.
    fallback: tuple[int, int] = (1170, 540)


class MenuStrategy(Strategy):
    name = "menu"

    def __init__(
        self,
        coords: MenuCoords | None = None,
        action_cooldown_s: float = 1.5,
        starting_timeout_s: float = 25.0,
        unknown_dismiss_after_s: float = 4.0,
        unknown_restart_after_s: float = 30.0,
        on_stuck_callback=None,
    ):
        self.coords = coords or MenuCoords()
        self.action_cooldown_s = action_cooldown_s
        self.starting_timeout_s = starting_timeout_s
        # If we're stuck in 'unknown' for this long, cycle through known
        # dismiss positions (home icon, OK center, Android back).
        self.unknown_dismiss_after_s = unknown_dismiss_after_s
        # If still unknown after this long, force-restart Brawl Stars.
        self.unknown_restart_after_s = unknown_restart_after_s
        self.on_stuck_callback = on_stuck_callback  # called when stuck
        self._last_action_at = 0.0
        self._last_state: str | None = None
        self._starting_since: float | None = None
        self._unknown_since: float | None = None
        # Rotation of dismiss strategies for unknown popups.
        self._unknown_attempt_idx = 0

    def decide(self, gs: GameState) -> Action | None:
        now = time.monotonic()

        # Track time spent in 'starting' (daily star drop trap, etc.).
        # Reset unknown timer when we move to a known state.
        if gs.state != "unknown":
            self._unknown_since = None
            self._unknown_attempt_idx = 0

        if gs.state == "starting":
            if self._starting_since is None:
                self._starting_since = now
                logger.info("MenuStrategy: entered 'starting' state, watching for stuck")
            elif (now - self._starting_since) > self.starting_timeout_s:
                logger.warning(
                    "MenuStrategy: stuck in 'starting' for %.0fs — requesting app restart",
                    now - self._starting_since,
                )
                self._starting_since = None
                if self.on_stuck_callback:
                    self.on_stuck_callback()
                # Reset cooldown so next state action fires immediately after restart.
                self._last_state = None
                return None
            # While in starting, try a tap on the center of screen periodically
            # (some popups dismiss with a tap anywhere).
            if (now - self._last_action_at) >= self.action_cooldown_s:
                self._last_action_at = now
                return Action.tap(*self.coords.fallback)
            return None
        else:
            self._starting_since = None

        # Throttle: avoid spamming the same button if the state hasn't changed.
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
        elif gs.state == "unknown":
            # When stuck on an unrecognized screen (event banners, season
            # popups, daily offers...) we cycle through known dismiss
            # positions until one frees us back to lobby/match.
            if self._unknown_since is None:
                self._unknown_since = now
                self._unknown_attempt_idx = 0
            elapsed = now - self._unknown_since
            if elapsed > self.unknown_restart_after_s and self.on_stuck_callback:
                logger.warning(
                    "MenuStrategy: stuck in 'unknown' for %.0fs — restarting app", elapsed
                )
                self._unknown_since = None
                self._unknown_attempt_idx = 0
                self.on_stuck_callback()
                self._last_state = None
                return None
            if elapsed > self.unknown_dismiss_after_s:
                if (now - self._last_action_at) >= self.action_cooldown_s:
                    # Sequence of dismiss positions (rotate on each retry).
                    targets = [
                        self.coords.popup_close,       # top-right home
                        (1170, 970),                   # bottom-center OK button
                        self.coords.fallback,          # screen center
                        (75, 35),                      # top-left back arrow
                    ]
                    t = targets[self._unknown_attempt_idx % len(targets)]
                    self._unknown_attempt_idx += 1
                    self._last_action_at = now
                    logger.info(
                        "MenuStrategy: unknown screen, dismiss attempt #%d at %s",
                        self._unknown_attempt_idx, t,
                    )
                    return Action.tap(*t)

        if action is not None:
            self._last_action_at = now
            self._last_state = gs.state
        return action
