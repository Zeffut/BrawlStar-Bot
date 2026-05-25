"""ColtStrategy — combat decisions for Colt in Solo Showdown.

Decision priorities (each tick, in order):

1. Survive: if low HP and an enemy is close, flee.
2. Escape danger zone (shrinking ring).
3. Opportunistic shot: if any enemy is in `attack_range` with a clear line of
   sight, fire at the closest such enemy.
4. Super: if 100% charged and at least one enemy in `super_range` with line of
   sight (or enemies cluster), fire super at the densest enemy area.
5. Power cube: if a cube is within ~300 px and no enemy threatens us, walk to it.
6. Kite: maintain `safe_range` from the nearest enemy.
7. Drift toward map center if no info.

Brawler info pulled from `data/brawlers_info.json`.

v1 simplifications:
- HP & super charge are NOT yet read from the screen → we assume max HP and
  no super (skipping rules 1 and 4 until we have UI reading). The decision
  tree is fully wired; flipping these on later is just plugging in real values.
- Danger zone NOT yet detected → rule 2 is a stub.
- Pathfinding is NOT yet tile-based — just a direct vector toward the target
  (good enough for an open map like Solo Showdown most of the time).
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path

from bsbot.controls.inputs import Action
from bsbot.strategies.base import Strategy
from bsbot.utils.geometry import (
    BBox,
    distance,
    line_of_sight,
    normalize,
)
from bsbot.utils.pathfinding import GridSpec, next_waypoint_vector
from bsbot.vision.postprocess import Enemy, GameState

logger = logging.getLogger(__name__)


@dataclass
class BrawlerStats:
    """Subset of brawlers_info.json relevant to combat decisions."""

    safe_range: float
    attack_range: float
    super_range: float
    super_type: str
    ignore_walls_for_attacks: bool
    ignore_walls_for_supers: bool

    @classmethod
    def load(cls, brawlers_info_path: str | Path, brawler_name: str) -> "BrawlerStats":
        data = json.loads(Path(brawlers_info_path).read_text())
        if brawler_name not in data:
            raise KeyError(f"Brawler '{brawler_name}' not in {brawlers_info_path}")
        b = data[brawler_name]
        return cls(
            safe_range=float(b["safe_range"]),
            attack_range=float(b["attack_range"]),
            super_range=float(b["super_range"]),
            super_type=b["super_type"],
            ignore_walls_for_attacks=bool(b["ignore_walls_for_attacks"]),
            ignore_walls_for_supers=bool(b["ignore_walls_for_supers"]),
        )


@dataclass
class ColtTuning:
    """Heuristic constants — tune via config later."""

    flee_hp_threshold_pct: float = 30.0
    flee_enemy_distance_px: float = 400.0
    cube_pickup_radius_px: float = 300.0
    cube_pickup_safe_radius_px: float = 400.0
    use_super_min_enemies_in_line: int = 1  # 1 enemy enough for now (Colt super is long range)
    drift_toward_center: bool = True
    pathfinding_cell_size: int = 48  # px per A* cell; smaller = finer = slower


class ColtStrategy(Strategy):
    name = "colt"

    def __init__(
        self,
        stats: BrawlerStats,
        tuning: ColtTuning | None = None,
    ):
        self.stats = stats
        self.tuning = tuning or ColtTuning()

    # ------------------------------------------------------------------ decide

    def decide(self, gs: GameState) -> Action | None:
        if gs.state != "match" or gs.my_pos is None:
            return None  # Not in a match or we don't see ourselves.

        # 1. Survive (skip if HP unknown — v1 placeholder).
        if gs.my_health_pct is not None and gs.my_health_pct < self.tuning.flee_hp_threshold_pct:
            flee = self._flee_from_nearest_enemy(gs)
            if flee is not None:
                return flee

        # 2. Escape danger zone — TODO when we detect the ring.

        # 3. Opportunistic shot.
        shoot = self._maybe_shoot(gs)
        if shoot is not None:
            return shoot

        # 4. Super.
        if gs.my_super_charge_pct is not None and gs.my_super_charge_pct >= 100.0:
            sup = self._maybe_super(gs)
            if sup is not None:
                return sup

        # 5. Power cube pickup.
        cube_move = self._maybe_pickup_cube(gs)
        if cube_move is not None:
            return cube_move

        # 6. Kite.
        kite = self._kite(gs)
        if kite is not None:
            return kite

        # 7. Drift toward center.
        if self.tuning.drift_toward_center:
            return self._move_toward(gs, (gs.frame_width // 2, gs.frame_height // 2))

        return Action.joystick_release()

    # ------------------------------------------------------------------ helpers

    def _flee_from_nearest_enemy(self, gs: GameState) -> Action | None:
        if not gs.enemies or gs.my_pos is None:
            return None
        nearest = min(gs.enemies, key=lambda e: distance(gs.my_pos, e.position))
        if distance(gs.my_pos, nearest.position) > self.tuning.flee_enemy_distance_px:
            return None
        # Flee in the opposite direction.
        dx = gs.my_pos[0] - nearest.position[0]
        dy = gs.my_pos[1] - nearest.position[1]
        ux, uy = normalize((dx, dy))
        return Action.joystick_move(ux, uy)

    def _enemies_in_range(self, gs: GameState, max_dist: float, *, respect_walls: bool) -> list[Enemy]:
        if gs.my_pos is None:
            return []
        out = []
        for e in gs.enemies:
            d = distance(gs.my_pos, e.position)
            if d > max_dist:
                continue
            if respect_walls and not line_of_sight(gs.my_pos, e.position, gs.walls):
                continue
            out.append(e)
        return out

    def _maybe_shoot(self, gs: GameState) -> Action | None:
        targets = self._enemies_in_range(
            gs,
            max_dist=self.stats.attack_range,
            respect_walls=not self.stats.ignore_walls_for_attacks,
        )
        if not targets:
            return None
        assert gs.my_pos is not None
        target = min(targets, key=lambda e: distance(gs.my_pos, e.position))
        return Action.aimed_attack(*target.position)

    def _maybe_super(self, gs: GameState) -> Action | None:
        targets = self._enemies_in_range(
            gs,
            max_dist=self.stats.super_range,
            respect_walls=not self.stats.ignore_walls_for_supers,
        )
        if len(targets) < self.tuning.use_super_min_enemies_in_line:
            return None
        assert gs.my_pos is not None
        # Pick the target maximizing potential — for v1 just nearest in line.
        target = min(targets, key=lambda e: distance(gs.my_pos, e.position))
        return Action.aimed_super(*target.position)

    def _maybe_pickup_cube(self, gs: GameState) -> Action | None:
        if not gs.power_cubes or gs.my_pos is None:
            return None
        # Find closest cube within pickup radius.
        cube = min(gs.power_cubes, key=lambda c: distance(gs.my_pos, c.center))
        if distance(gs.my_pos, cube.center) > self.tuning.cube_pickup_radius_px:
            return None
        # Check no enemy too close to interfere.
        nearest_enemy_d = (
            min((distance(gs.my_pos, e.position) for e in gs.enemies), default=float("inf"))
        )
        if nearest_enemy_d < self.tuning.cube_pickup_safe_radius_px:
            return None
        return self._move_toward(gs, cube.center)

    def _kite(self, gs: GameState) -> Action | None:
        if not gs.enemies or gs.my_pos is None:
            return None
        nearest = min(gs.enemies, key=lambda e: distance(gs.my_pos, e.position))
        d = distance(gs.my_pos, nearest.position)
        # Try to stay at safe_range.
        if d < self.stats.safe_range * 0.9:
            # Too close — back off.
            dx = gs.my_pos[0] - nearest.position[0]
            dy = gs.my_pos[1] - nearest.position[1]
            ux, uy = normalize((dx, dy))
            return Action.joystick_move(ux, uy)
        if d > self.stats.safe_range * 1.5:
            # Too far — close the gap a bit.
            dx = nearest.position[0] - gs.my_pos[0]
            dy = nearest.position[1] - gs.my_pos[1]
            ux, uy = normalize((dx, dy))
            return Action.joystick_move(ux, uy)
        return None  # Already at good distance, no kite move needed.

    def _move_toward(self, gs: GameState, target: tuple[int, int]) -> Action:
        """Move toward `target`. Uses A* around walls if walls are present and
        line of sight to target is blocked; otherwise direct vector.
        """
        assert gs.my_pos is not None
        # Fast path: no walls or LoS clear → direct vector.
        if not gs.walls or line_of_sight(gs.my_pos, target, gs.walls):
            dx = target[0] - gs.my_pos[0]
            dy = target[1] - gs.my_pos[1]
            ux, uy = normalize((dx, dy))
            return Action.joystick_move(ux, uy)

        # Walls block direct path → A*.
        spec = GridSpec.for_frame(
            gs.frame_width or 1920,
            gs.frame_height or 1080,
            cell_size=self.tuning.pathfinding_cell_size,
        )
        v = next_waypoint_vector(spec, gs.walls, gs.my_pos, target)
        if v is None:
            # No path — fall back to direct vector (better than freezing).
            dx = target[0] - gs.my_pos[0]
            dy = target[1] - gs.my_pos[1]
            ux, uy = normalize((dx, dy))
            return Action.joystick_move(ux, uy)
        return Action.joystick_move(v[0], v[1])
