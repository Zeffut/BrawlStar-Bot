"""Strategy for the "push account to max trophies" mode.

Smart rotation:
  - Each brawler has an `expected_gain` derived from its current trophies
    (Brawl Stars rewards taper above ~500). The picker always serves the
    brawler with the highest expected gain.
  - After N consecutive defeats with a brawler we mark it `exhausted`
    and never pick it again in this session (next pick goes to the
    second-best).
  - When every brawler is exhausted, the session is complete.

The mode does not "wait" or "cooldown" — Brawl Stars defeats don't rest
themselves, so reusing an exhausted brawler in the same session is
strictly negative. A separate run later (different session) starts
fresh because brawlace re-reads the account's current trophies.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

log = logging.getLogger(__name__)

# Defeats in a row before we abandon a brawler.
DEFAULT_DEFEAT_LIMIT = 3


def expected_gain(trophies: int) -> float:
    """Approximate net trophies / match assuming a 50% win rate.

    Brawl Stars wins give +10 at low trophies, dropping to ~+3 above
    1000; losses cost -2 at low, scaling up to -11 above 1000. Net
    gain at 50% WR therefore drops smoothly from +4 to -4 across that
    range.
    """
    if trophies < 300:   return 4.0
    if trophies < 500:   return 3.0
    if trophies < 600:   return 1.5
    if trophies < 700:   return 0.5
    if trophies < 800:   return -1.0
    if trophies < 900:   return -2.0
    return -4.0


@dataclass
class BrawlerState:
    name: str
    trophies: int
    defeat_streak: int = 0
    matches_played: int = 0
    exhausted: bool = False


@dataclass
class PushMaxStrategy:
    """Owns the per-brawler state for a push-max session."""
    brawlers: dict[str, BrawlerState] = field(default_factory=dict)
    defeat_limit: int = DEFAULT_DEFEAT_LIMIT
    current: str | None = None

    @classmethod
    def from_owned(cls, owned: list[dict], defeat_limit: int = DEFAULT_DEFEAT_LIMIT) -> "PushMaxStrategy":
        """Build a strategy from the brawlace `fetch_owned_brawlers` list."""
        state = cls(defeat_limit=defeat_limit)
        for b in owned:
            state.brawlers[b["name"]] = BrawlerState(
                name=b["name"], trophies=b.get("trophies", 0),
            )
        log.info("PushMax built with %d brawlers", len(state.brawlers))
        return state

    def pick_next(self) -> BrawlerState | None:
        """Return the best brawler to play right now (highest expected
        gain among non-exhausted ones). Returns None when done."""
        candidates = [b for b in self.brawlers.values() if not b.exhausted]
        if not candidates:
            return None
        # Sort by expected_gain desc, tiebreak by trophies (push the
        # higher-trophy one first since it's "almost done").
        candidates.sort(key=lambda b: (-expected_gain(b.trophies), -b.trophies))
        winner = candidates[0]
        self.current = winner.name
        log.info("PushMax pick: %s (trophies=%d, gain=%.1f, remaining=%d)",
                 winner.name, winner.trophies, expected_gain(winner.trophies),
                 len(candidates))
        return winner

    def record_match(self, brawler: str, result: str,
                     trophies_after: int) -> None:
        """Update state after a match. If the defeat limit is hit,
        the brawler is marked exhausted (won't be picked again)."""
        b = self.brawlers.get(brawler)
        if b is None:
            log.warning("record_match: unknown brawler %r", brawler)
            return
        b.trophies = trophies_after
        b.matches_played += 1
        if result == "defeat":
            b.defeat_streak += 1
        else:
            b.defeat_streak = 0  # any non-defeat resets
        if b.defeat_streak >= self.defeat_limit:
            b.exhausted = True
            log.info("PushMax: %s EXHAUSTED after %d defeats (trophies=%d)",
                     b.name, b.defeat_streak, b.trophies)

    def all_done(self) -> bool:
        return all(b.exhausted for b in self.brawlers.values())

    def summary(self) -> dict:
        active = [b.name for b in self.brawlers.values() if not b.exhausted]
        exhausted = [b.name for b in self.brawlers.values() if b.exhausted]
        total_trophies = sum(b.trophies for b in self.brawlers.values())
        return {
            "current": self.current,
            "active_count": len(active),
            "exhausted_count": len(exhausted),
            "exhausted": exhausted,
            "total_trophies": total_trophies,
        }
