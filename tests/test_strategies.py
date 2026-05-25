"""Tests for MenuStrategy and ColtStrategy."""
from __future__ import annotations

import time

import pytest

from bsbot.controls.inputs import Action, ActionType
from bsbot.strategies.colt import BrawlerStats, ColtStrategy, ColtTuning
from bsbot.strategies.menu import MenuCoords, MenuStrategy
from bsbot.utils.geometry import BBox
from bsbot.vision.postprocess import Enemy, GameState


# ---------------- MenuStrategy ----------------

class TestMenuStrategy:
    def test_lobby_taps_play(self):
        s = MenuStrategy(MenuCoords(play_button=(100, 200)))
        gs = GameState(state="lobby")
        a = s.decide(gs)
        assert a is not None
        assert a.type == ActionType.TAP and (a.x, a.y) == (100, 200)

    def test_end_taps_continue(self):
        s = MenuStrategy(MenuCoords(continue_button=(300, 400)))
        gs = GameState(state="end")
        a = s.decide(gs)
        assert a is not None and (a.x, a.y) == (300, 400)

    def test_disconnect_taps_reconnect(self):
        s = MenuStrategy(MenuCoords(reconnect_button=(500, 600)))
        a = s.decide(GameState(state="disconnect"))
        assert a is not None and (a.x, a.y) == (500, 600)

    def test_unknown_does_nothing(self):
        s = MenuStrategy()
        assert s.decide(GameState(state="unknown")) is None

    def test_starting_does_nothing(self):
        s = MenuStrategy()
        assert s.decide(GameState(state="starting")) is None

    def test_throttles_repeated_state(self):
        s = MenuStrategy(action_cooldown_s=0.2)
        gs = GameState(state="lobby")
        first = s.decide(gs)
        assert first is not None
        # Immediate second call same state -> throttled.
        assert s.decide(gs) is None
        time.sleep(0.25)
        # After cooldown, fires again.
        assert s.decide(gs) is not None

    def test_state_change_breaks_throttle(self):
        s = MenuStrategy(action_cooldown_s=5.0)
        s.decide(GameState(state="lobby"))
        # Different state immediately should fire.
        assert s.decide(GameState(state="end")) is not None


# ---------------- ColtStrategy ----------------

@pytest.fixture
def colt_stats() -> BrawlerStats:
    return BrawlerStats(
        safe_range=324.0,
        attack_range=546.0,
        super_range=704.0,
        super_type="damage",
        ignore_walls_for_attacks=False,
        ignore_walls_for_supers=False,
    )


@pytest.fixture
def colt(colt_stats) -> ColtStrategy:
    return ColtStrategy(colt_stats, tuning=ColtTuning(drift_toward_center=False))


def _gs_match(my_pos, enemies=None, walls=None, cubes=None, hp=None, super_pct=None, w=1920, h=1080):
    return GameState(
        state="match",
        my_pos=my_pos,
        enemies=enemies or [],
        walls=walls or [],
        bushes=[],
        power_cubes=cubes or [],
        my_health_pct=hp,
        my_super_charge_pct=super_pct,
        frame_width=w,
        frame_height=h,
    )


class TestColtStrategyBasic:
    def test_not_match_returns_none(self, colt):
        assert colt.decide(GameState(state="lobby")) is None

    def test_no_my_pos_returns_none(self, colt):
        assert colt.decide(GameState(state="match", my_pos=None)) is None

    def test_no_enemies_drift_disabled_releases(self, colt):
        a = colt.decide(_gs_match(my_pos=(960, 540)))
        assert a is not None and a.type == ActionType.JOYSTICK_RELEASE


class TestColtStrategyShoot:
    def test_shoots_enemy_in_range_with_los(self, colt):
        enemy = Enemy(bbox=BBox(900, 400, 1000, 500), brawler_class="shelly", confidence=1.0)
        a = colt.decide(_gs_match(my_pos=(500, 500), enemies=[enemy]))
        assert a is not None
        assert a.type == ActionType.AIMED_ATTACK
        # Target should be enemy center (~950, 450).
        assert a.x == 950 and a.y == 450

    def test_does_not_shoot_enemy_out_of_range(self, colt):
        enemy = Enemy(bbox=BBox(1500, 400, 1600, 500), brawler_class="shelly", confidence=1.0)
        # distance ~1050 > attack_range 546
        a = colt.decide(_gs_match(my_pos=(500, 500), enemies=[enemy]))
        # Should NOT be an attack — should be a kite move (enemy too far, close gap).
        assert a is None or a.type != ActionType.AIMED_ATTACK

    def test_does_not_shoot_enemy_behind_wall(self, colt):
        # Wall on the path.
        wall = BBox(700, 200, 750, 800)
        enemy = Enemy(bbox=BBox(900, 400, 1000, 500), brawler_class="shelly", confidence=1.0)
        a = colt.decide(_gs_match(my_pos=(500, 500), enemies=[enemy], walls=[wall]))
        assert a is None or a.type != ActionType.AIMED_ATTACK

    def test_picks_nearest_enemy(self, colt):
        close = Enemy(bbox=BBox(700, 450, 800, 550), brawler_class="bull", confidence=1.0)
        far = Enemy(bbox=BBox(900, 400, 1000, 500), brawler_class="shelly", confidence=1.0)
        a = colt.decide(_gs_match(my_pos=(500, 500), enemies=[close, far]))
        assert a is not None and a.type == ActionType.AIMED_ATTACK
        # Should target the closer one.
        assert a.x == 750  # close enemy center x


class TestColtStrategySuper:
    def test_no_super_below_100(self, colt):
        e = Enemy(bbox=BBox(800, 450, 900, 550), brawler_class="shelly", confidence=1.0)
        a = colt.decide(_gs_match(my_pos=(500, 500), enemies=[e], super_pct=80))
        # Above attack range? 850-500 = 350 < 546 -> would still shoot first.
        assert a is not None and a.type == ActionType.AIMED_ATTACK

    def test_super_at_100_and_target_out_of_attack_range(self, colt):
        # Place an enemy beyond attack_range but within super_range.
        e = Enemy(bbox=BBox(1100, 400, 1200, 500), brawler_class="shelly", confidence=1.0)
        # distance 650 > attack 546, < super 704
        a = colt.decide(_gs_match(my_pos=(500, 500), enemies=[e], super_pct=100))
        assert a is not None
        assert a.type == ActionType.AIMED_SUPER


class TestColtStrategyKite:
    def test_too_close_backs_off(self, colt):
        # Enemy within safe_range * 0.9 = ~290.
        e = Enemy(bbox=BBox(550, 500, 650, 600), brawler_class="bull", confidence=1.0)
        # distance ~150 < 290 -> should kite away. But also <attack range so will shoot first.
        a = colt.decide(_gs_match(my_pos=(500, 500), enemies=[e]))
        # Shoot is priority. Kite happens when no shot is possible.
        assert a is not None and a.type == ActionType.AIMED_ATTACK

    def test_kite_when_enemy_behind_wall_and_too_close(self, colt):
        # Behind wall (can't shoot) and too close (will back off).
        wall = BBox(550, 200, 600, 800)
        e = Enemy(bbox=BBox(620, 480, 720, 580), brawler_class="bull", confidence=1.0)  # ~170 away
        a = colt.decide(_gs_match(my_pos=(500, 500), enemies=[e], walls=[wall]))
        assert a is not None
        assert a.type == ActionType.JOYSTICK_MOVE
        # Should move away from enemy: dx negative (left).
        assert a.dx is not None and a.dx < 0


class TestColtStrategyCubes:
    def test_picks_up_close_cube(self, colt):
        cube = BBox(540, 520, 580, 560)  # ~52 px away
        a = colt.decide(_gs_match(my_pos=(500, 500), cubes=[cube]))
        assert a is not None
        assert a.type == ActionType.JOYSTICK_MOVE

    def test_ignores_cube_when_enemy_close(self, colt):
        cube = BBox(540, 520, 580, 560)
        enemy = Enemy(bbox=BBox(700, 500, 800, 600), brawler_class="bull", confidence=1.0)  # ~250 away
        # Will shoot enemy instead of pickup (within attack range).
        a = colt.decide(_gs_match(my_pos=(500, 500), cubes=[cube], enemies=[enemy]))
        assert a is not None and a.type == ActionType.AIMED_ATTACK


class TestBrawlerStatsLoading:
    def test_loads_colt_from_real_json(self):
        # Use the actual brawlers_info.json shipped with the project.
        from pathlib import Path
        p = Path(__file__).parent.parent / "src" / "bsbot" / "data" / "brawlers_info.json"
        stats = BrawlerStats.load(p, "colt")
        assert stats.attack_range > 0
        assert stats.super_range > 0
