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

    # Calibrated for BlueStacks (2560x1440 landscape) on 2026-05-25.
    play_button: tuple[int, int] = (2295, 1340)
    continue_button: tuple[int, int] = (2295, 1340)
    popup_close: tuple[int, int] = (2470, 80)
    reconnect_button: tuple[int, int] = (700, 900)
    # Generic safe-tap when stuck — center of screen.
    fallback: tuple[int, int] = (1280, 720)


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
        # Track entry time for every non-gameplay state, to escalate to
        # app-restart if any dismiss strategy fails to free us.
        self._state_entered_at: float = 0.0
        self._stuck_restart_after_s: float = 25.0
        # Absolute timer: time since we last saw a "safe" gameplay state
        # (lobby or match). Independent of state oscillation — when this
        # exceeds the threshold, force app restart.
        self._last_gameplay_at: float = 0.0
        self._no_gameplay_restart_after_s: float = 60.0

    def decide(self, gs: GameState) -> Action | None:
        now = time.monotonic()

        # Track time spent in 'starting' (daily star drop trap, etc.).
        # Reset unknown timer when we move to a known state.
        if gs.state != "unknown":
            self._unknown_since = None
        # Reset dismiss rotation index only when we ESCAPE into a true gameplay
        # state (lobby or match). 'end' is itself a dismiss state — resetting
        # there would lock us on the first dismiss position forever.
        if gs.state in ("lobby", "match"):
            self._unknown_attempt_idx = 0

        # Track when we entered the current non-gameplay state.
        if gs.state != self._last_state:
            self._state_entered_at = now
        # Update last-seen-gameplay timestamp.
        if gs.state in ("lobby", "match"):
            self._last_gameplay_at = now
        elif self._last_gameplay_at == 0.0:
            # First non-gameplay state encountered — anchor the timer here.
            self._last_gameplay_at = now

        # Restart trigger A: stuck on a single non-gameplay state for too long.
        stuck_in_state = (
            gs.state in ("disconnect", "popup", "end", "starting")
            and (now - self._state_entered_at) > self._stuck_restart_after_s
        )
        # Restart trigger B: been bouncing without reaching lobby/match for too
        # long (resilient to state oscillation).
        no_gameplay = (
            self._last_gameplay_at > 0.0
            and (now - self._last_gameplay_at) > self._no_gameplay_restart_after_s
        )
        if (stuck_in_state or no_gameplay) and self.on_stuck_callback:
            reason = "single-state stuck" if stuck_in_state else "no-gameplay timeout"
            logger.warning(
                "MenuStrategy: %s in state %r (single=%.0fs, no_gp=%.0fs) — restarting app",
                reason, gs.state,
                now - self._state_entered_at,
                now - self._last_gameplay_at,
            )
            self.on_stuck_callback()
            self._state_entered_at = now
            self._last_gameplay_at = now
            self._last_state = None
            return None

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
            # VICTOIRE screen has a single CONTINUER button (2130, 1000).
            # DÉFAITE screen has REJOUER (1990, 1000) + QUITTER (2230, 1000).
            # Rotate to make sure we hit something dismissive.
            targets = [
                self.coords.continue_button,   # CONTINUER (VICTOIRE)
                (2230, 1000),                  # QUITTER (DÉFAITE)
                (1990, 1000),                  # REJOUER (DÉFAITE)
                self.coords.popup_close,       # home icon (fallback)
            ]
            t = targets[self._unknown_attempt_idx % len(targets)]
            self._unknown_attempt_idx += 1
            action = Action.tap(*t)
        elif gs.state == "popup":
            # Different popups have different dismiss buttons (home icon,
            # green OK, back arrow). Rotate through known positions until
            # one dismisses.
            targets = [
                self.coords.popup_close,       # top-right home icon
                (1170, 970),                   # green OK button bottom-center
                self.coords.fallback,          # screen center
                (75, 35),                      # top-left back arrow
            ]
            t = targets[self._unknown_attempt_idx % len(targets)]
            self._unknown_attempt_idx += 1
            action = Action.tap(*t)
        elif gs.state == "disconnect":
            # Try RECHARGER first; if disconnect persists (e.g. AFK kick on
            # post-match DÉFAITE/VICTOIRE where reconnect does nothing),
            # cycle through QUITTER/REJOUER/home positions.
            targets = [
                self.coords.reconnect_button,  # RECHARGER link
                (2230, 1000),                  # QUITTER button bottom-right
                (1990, 1000),                  # REJOUER button (left of QUITTER)
                self.coords.popup_close,       # home icon top-right
            ]
            t = targets[self._unknown_attempt_idx % len(targets)]
            self._unknown_attempt_idx += 1
            action = Action.tap(*t)
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
