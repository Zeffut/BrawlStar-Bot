"""Resale price estimation from extracted account data.

Pricing grid sourced from revente/grille_prix.md (benchmark 2026-05-31).
Amorçage prices (new-seller, -10/-15% under market) are used as the
default output range.
"""
# PEP 604 unions (int | None) in annotations need this on Python 3.9 (the
# Mac dev env runs 3.9; the HP worker runs 3.12). Without it the dataclass
# annotation is evaluated at class-definition time and raises TypeError,
# which broke pytest collection for the WHOLE suite.
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class AccountData:
    tag: str
    name: str
    trophies: int
    brawlers: int
    power11: int
    gems: int | None = None
    gold: int | None = None
    level: int | None = None
    rare_skins: int = 0     # confirmed rare/old skins (visual pass)
    hypercharges: int = 0   # unlocked hypercharges (visual pass)


@dataclass
class Estimate:
    tier: str               # "basique" | "chargé"
    price_min_usd: float
    price_max_usd: float
    confidence: str         # "low" | "medium" | "high"
    notes: list[str] = field(default_factory=list)


# (palier_min_trophies, basique (min, max), chargé (min, max)) — amorçage USD
_GRID = [
    (30000, (35, 45), (60, 90)),
    (25000, (20, 26), (40, 70)),
    (20000, (15, 19), (35, 60)),
    (15000, (9, 12),  (15, 25)),
    (0,     (3, 8),   (10, 25)),   # below the value grid
]


def _bucket(trophies: int):
    for floor, basique, charge in _GRID:
        if trophies >= floor:
            return basique, charge
    return (3, 8), (10, 25)


def _is_charged(data: AccountData) -> bool:
    return data.power11 >= 8 or data.hypercharges >= 3 or data.rare_skins >= 1


def estimate(data: AccountData) -> Estimate:
    basique, charge = _bucket(data.trophies)
    charged = _is_charged(data)
    tier = "chargé" if charged else "basique"
    lo, hi = charge if charged else basique

    notes: list[str] = []
    # Skins + hypercharges are the biggest price levers and are NOT
    # auto-measured reliably. If they weren't provided, the estimate is
    # indicative only and confidence is capped low.
    if data.hypercharges == 0 and data.rare_skins == 0:
        notes.append("skins/hypercharges non mesurés — fourchette indicative")
        confidence = "low"
    else:
        confidence = "medium"
    if data.trophies < 15000:
        notes.append("sous le seuil de valeur (15k) — marge quasi nulle après frais")

    return Estimate(tier=tier, price_min_usd=float(lo), price_max_usd=float(hi),
                    confidence=confidence, notes=notes)
