"""BrainWorker — read GameState, dispatch to the right Strategy, emit Actions."""
from __future__ import annotations

import logging
import threading

from bsbot.buses import ControlBus, LatestSlot
from bsbot.strategies.base import Strategy
from bsbot.vision.postprocess import GameState

logger = logging.getLogger(__name__)


class BrainWorker(threading.Thread):
    """Pull GameState updates, route to the matching Strategy, publish Actions.

    `strategies` maps **game state → Strategy**:
        {
            "match":    ColtStrategy(...),
            "lobby":    MenuStrategy(...),
            "end":      MenuStrategy(...),
            "popup":    MenuStrategy(...),
            "disconnect": MenuStrategy(...),
        }

    A single Strategy instance can be reused for multiple states (e.g. one
    MenuStrategy covers lobby/end/popup/disconnect).
    """

    def __init__(
        self,
        state_slot: LatestSlot[GameState],
        control_bus: ControlBus,
        stop_event: threading.Event,
        strategies: dict[str, Strategy],
        default_strategy: Strategy | None = None,
        tick_timeout_s: float = 0.5,
    ):
        super().__init__(name="BrainWorker", daemon=True)
        self.state_slot = state_slot
        self.control_bus = control_bus
        self.stop_event = stop_event
        self.strategies = strategies
        self.default_strategy = default_strategy
        self.tick_timeout_s = tick_timeout_s
        self._last_seen_version = 0

    def run(self) -> None:
        logger.info("BrainWorker starting")
        while not self.stop_event.is_set():
            gs, version = self.state_slot.wait_new(self._last_seen_version, timeout=self.tick_timeout_s)
            if gs is None or version == self._last_seen_version:
                continue
            self._last_seen_version = version
            try:
                action = self._dispatch(gs)
                if action is not None:
                    self.control_bus.put(action, block=False)
            except Exception:
                logger.exception("Strategy dispatch failed for state %s", gs.state)
        logger.info("BrainWorker stopped")

    def _dispatch(self, gs: GameState):
        strategy = self.strategies.get(gs.state, self.default_strategy)
        if strategy is None:
            return None
        return strategy.decide(gs)
