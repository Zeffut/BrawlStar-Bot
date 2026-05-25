"""Strategy interface — turns a GameState into an Action (or none)."""
from __future__ import annotations

from abc import ABC, abstractmethod

from bsbot.controls.inputs import Action
from bsbot.vision.postprocess import GameState


class Strategy(ABC):
    """A strategy decides what `Action` to perform given the current `GameState`.

    Strategies should be **stateless or near-stateless** when possible — easier
    to test. Persistent state (cooldowns, last-action timestamps) is acceptable
    when it's purely internal.

    Strategies must be **fast** (sub-millisecond ideally), as `decide()` is
    called on every BrainWorker tick.
    """

    name: str = "base"

    @abstractmethod
    def decide(self, gs: GameState) -> Action | None:
        """Return the action to perform, or None for no-op this tick."""
        ...
