"""Strategy for the "push account to max trophies" mode.

Philosophy: **focus, don't shuffle**.
  - Brawl Stars rewards no longer scale on a fixed ladder — the cap a
    bot can reach is purely a function of how well it plays each
    brawler. So we don't target per-brawler trophy counts.
  - Instead: pick the bot's strongest brawler first (Pyla skill tiers
    below), STAY on it until it stagnates (no net gain over last
    `STAGNATION_WINDOW` matches), mark exhausted, move to next.
  - Stop the whole session when all brawlers are exhausted OR the
    global trophy target (set by the user) is reached.

Skill tiers reflect the Pyla AI vision/control loop's strengths:
  - S: long-range projectile, large hitbox auto-aim friendly
  - A: medium-range reliable shots
  - B: melee / mid-skill positioning required
  - C: complex skill-shots / mechanics Pyla doesn't model well

Within a tier we play the highest-trophy brawler first (its loss curve
hurts more if we wait). Lower tiers only kick in after higher ones are
all exhausted, never as a fallback during the same brawler's grind.
"""
from __future__ import annotations

import collections
import logging
from dataclasses import dataclass, field

log = logging.getLogger(__name__)

DEFAULT_DEFEAT_LIMIT = 5      # safety net: never more than this many losses in a row
STAGNATION_WINDOW = 8         # net trophy delta watched over last N matches
STAGNATION_THRESHOLD = 0      # delta <= this over the window → exhausted


# Hand-picked tiers based on Pyla's known strengths/weaknesses.
# Anything not listed falls to tier "B" (default).
BRAWLER_TIERS: dict[str, str] = {
    # ----- S tier (Pyla excels: long range + simple aim) -----
    "brock": "S", "barley": "S", "piper": "S", "bea": "S",
    "penny": "S", "bo": "S", "byron": "S", "tick": "S",
    "8-bit": "S", "8bit": "S",
    # ----- A tier (medium range, reliable) -----
    "shelly": "A", "colt": "A", "nita": "A", "jessie": "A",
    "rico": "A", "spike": "A", "pam": "A", "poco": "A",
    "gene": "A", "tara": "A", "leon": "A", "amber": "A",
    "lou": "A", "ruffs": "A", "belle": "A", "squeak": "A",
    "lola": "A", "fang": "A", "maisie": "A", "bibi": "A",
    "carl": "A", "stu": "A",
    # ----- B tier (melee / positioning-heavy) -----
    "bull": "B", "el primo": "B", "frank": "B", "darryl": "B",
    "rosa": "B", "jacky": "B", "ash": "B", "edgar": "B",
    "buzz": "B", "sam": "B", "meg": "B", "mortis": "B",
    "max": "B", "crow": "B", "mr. p": "B", "mrp": "B",
    "mr p": "B", "emz": "B", "willow": "B", "doug": "B",
    "kit": "B", "buster": "B",
    # ----- C tier (poor fit for current Pyla logic) -----
    "dynamike": "C", "sandy": "C", "gale": "C", "lily": "C",
    "berry": "C", "chester": "C", "gus": "C", "janet": "C",
    "mandy": "C", "hank": "C", "pearl": "C", "eve": "C",
    "otis": "C", "griff": "C",
}
TIER_ORDER = {"S": 0, "A": 1, "B": 2, "C": 3}

# Above this trophy count, expected_gain() turns non-positive — matches lose
# as much as they gain, so pushing there barely grows the account total.
# push_max prioritises brawlers BELOW this ceiling ("easiest trophies").
EFFICIENCY_CEILING = 700


def get_tier(name: str) -> str:
    return BRAWLER_TIERS.get(name.lower().strip(), "B")


@dataclass
class BrawlerState:
    name: str
    trophies: int
    tier: str = "B"
    defeat_streak: int = 0
    matches_played: int = 0
    exhausted: bool = False
    # Rolling window of net trophy deltas (post - pre) per match.
    # Stagnation declared when sum(deltas) <= STAGNATION_THRESHOLD and
    # the window is full.
    deltas: collections.deque = field(
        default_factory=lambda: collections.deque(maxlen=STAGNATION_WINDOW)
    )
    start_trophies: int = 0     # snapshot when first picked
    locked: bool = False        # picked at least once → stay until exhausted


@dataclass
class PushMaxStrategy:
    """Owns the per-brawler state for a push-max session.

    `current` is the brawler currently being played. We do NOT switch
    away from it until it's marked exhausted, even if another brawler
    looks juicier — the user wants focus, not optimization noise.
    """
    brawlers: dict[str, BrawlerState] = field(default_factory=dict)
    defeat_limit: int = DEFAULT_DEFEAT_LIMIT
    stagnation_window: int = STAGNATION_WINDOW
    current: str | None = None
    # Global per-brawler trophy cap: when a brawler reaches this many
    # trophies, it's marked exhausted and the bot moves to the next one.
    # None = no cap (push until stagnation, the original behavior).
    brawler_max_trophies: int | None = None

    @classmethod
    def from_owned(cls, owned: list[dict],
                   defeat_limit: int = DEFAULT_DEFEAT_LIMIT,
                   brawler_max_trophies: int | None = None) -> "PushMaxStrategy":
        """Build a strategy from the brawlace `fetch_owned_brawlers` list."""
        state = cls(defeat_limit=defeat_limit,
                    brawler_max_trophies=brawler_max_trophies)
        for b in owned:
            name = b["name"]
            bs = BrawlerState(
                name=name,
                trophies=b.get("trophies", 0),
                tier=get_tier(name),
            )
            # Already at/above the cap → don't bother picking it.
            if brawler_max_trophies is not None and bs.trophies >= brawler_max_trophies:
                bs.exhausted = True
            state.brawlers[name] = bs
        log.info("PushMax built with %d brawlers (S=%d A=%d B=%d C=%d)",
                 len(state.brawlers),
                 sum(1 for b in state.brawlers.values() if b.tier == "S"),
                 sum(1 for b in state.brawlers.values() if b.tier == "A"),
                 sum(1 for b in state.brawlers.values() if b.tier == "B"),
                 sum(1 for b in state.brawlers.values() if b.tier == "C"))
        return state

    def pick_next(self) -> BrawlerState | None:
        """Return the brawler to play, prioritising the *easiest* trophies.

        Push brawlers with positive expected gain first (below the
        diminishing-returns ceiling) — that grows the account total fastest.
        A brawler above the ceiling (e.g. a 900+ brock, where a match loses
        more than it gains) is only touched if nothing easier is left.
        Stickiness: keep the current brawler while it's still below the
        ceiling and not exhausted.
        """
        if self.current:
            cur = self.brawlers.get(self.current)
            if cur and not cur.exhausted and cur.trophies < EFFICIENCY_CEILING:
                return cur
        candidates = [b for b in self.brawlers.values() if not b.exhausted]
        if not candidates:
            return None
        easy = [b for b in candidates if b.trophies < EFFICIENCY_CEILING]
        pool = easy if easy else candidates
        # Highest expected gain (lowest trophies) first; tie-break by tier.
        pool.sort(key=lambda b: (-expected_gain(b.trophies), TIER_ORDER.get(b.tier, 99)))
        winner = pool[0]
        self.current = winner.name
        if not winner.locked:
            winner.locked = True
            winner.start_trophies = winner.trophies
        log.info("PushMax pick: %s [%s tier] trophies=%d (remaining=%d)",
                 winner.name, winner.tier, winner.trophies, len(candidates))
        return winner

    def record_match(self, brawler: str, result: str,
                     trophies_after: int) -> None:
        """Update state after a match.

        Marks brawler `exhausted` when ANY of:
          - defeat_streak >= defeat_limit (5 losses in a row — safety net)
          - rolling window full AND net delta over window <= 0
        """
        b = self.brawlers.get(brawler)
        if b is None:
            log.warning("record_match: unknown brawler %r", brawler)
            return
        delta = trophies_after - b.trophies
        b.trophies = trophies_after
        b.matches_played += 1
        b.deltas.append(delta)
        # Per-brawler trophy cap reached → done with this brawler, move on.
        if (self.brawler_max_trophies is not None
                and b.trophies >= self.brawler_max_trophies):
            b.exhausted = True
            log.info("PushMax: %s reached cap (%d/%d) — moving to next brawler",
                     b.name, b.trophies, self.brawler_max_trophies)
            return
        if result == "defeat":
            b.defeat_streak += 1
        else:
            b.defeat_streak = 0
        # Safety-net defeat streak.
        if b.defeat_streak >= self.defeat_limit:
            b.exhausted = True
            log.info("PushMax: %s EXHAUSTED (defeat_streak=%d, trophies=%d)",
                     b.name, b.defeat_streak, b.trophies)
            return
        # Stagnation over rolling window.
        if len(b.deltas) >= self.stagnation_window:
            window_delta = sum(b.deltas)
            if window_delta <= STAGNATION_THRESHOLD:
                b.exhausted = True
                log.info("PushMax: %s EXHAUSTED via stagnation "
                         "(last %d matches net=%+d, trophies=%d)",
                         b.name, self.stagnation_window, window_delta, b.trophies)

    def all_done(self) -> bool:
        return all(b.exhausted for b in self.brawlers.values())

    def summary(self) -> dict:
        active = [b.name for b in self.brawlers.values() if not b.exhausted]
        exhausted = [b.name for b in self.brawlers.values() if b.exhausted]
        total_trophies = sum(b.trophies for b in self.brawlers.values())
        cur = self.brawlers.get(self.current) if self.current else None
        return {
            "current": self.current,
            "current_tier": cur.tier if cur else None,
            "active_count": len(active),
            "exhausted_count": len(exhausted),
            "exhausted": exhausted,
            "total_trophies": total_trophies,
        }


# Back-compat alias for old code that imported `expected_gain`. Kept so
# the change is non-breaking; not used by the new strategy.
def expected_gain(trophies: int) -> float:
    if trophies < 300:   return 4.0
    if trophies < 500:   return 3.0
    if trophies < 600:   return 1.5
    if trophies < 700:   return 0.5
    if trophies < 800:   return -1.0
    if trophies < 900:   return -2.0
    return -4.0
