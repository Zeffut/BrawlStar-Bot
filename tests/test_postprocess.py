"""Tests for vision/postprocess.py — parsing raw detections to GameState."""
from __future__ import annotations

from bsbot.utils.geometry import BBox
from bsbot.vision.postprocess import (
    build_match_state,
    parse_brawler_detections,
    parse_power_cubes,
    parse_tile_detections,
)


class TestParseTiles:
    def test_splits_walls_and_bushes(self):
        raw = {
            "wall": [[0, 0, 10, 10], [50, 50, 60, 60]],
            "bush": [[100, 100, 120, 120]],
            "close_bush": [[200, 200, 220, 220]],
        }
        walls, bushes = parse_tile_detections(raw)
        assert len(walls) == 2
        assert len(bushes) == 2
        assert all(isinstance(b, BBox) for b in walls + bushes)

    def test_unknown_classes_ignored(self):
        raw = {"random": [[0, 0, 10, 10]]}
        walls, bushes = parse_tile_detections(raw)
        assert walls == []
        assert bushes == []

    def test_empty(self):
        walls, bushes = parse_tile_detections({})
        assert walls == []
        assert bushes == []


class TestParseBrawlers:
    def test_player_class_gives_my_pos(self):
        raw = {"player": [[400, 500, 600, 700]]}
        my_pos, enemies = parse_brawler_detections(raw)
        assert my_pos == (500, 600)
        assert enemies == []

    def test_enemy_class_becomes_enemies(self):
        raw = {"enemy": [[100, 100, 200, 200], [800, 800, 900, 900]]}
        _, enemies = parse_brawler_detections(raw, frame_width=1000, frame_height=1000)
        assert len(enemies) == 2

    def test_teammate_class_ignored(self):
        raw = {"teammate": [[100, 100, 200, 200]]}
        _, enemies = parse_brawler_detections(raw, frame_width=1000, frame_height=1000)
        assert enemies == []

    def test_my_pos_falls_back_to_center(self):
        raw = {"enemy": [[100, 100, 200, 200]]}
        my_pos, _ = parse_brawler_detections(raw, frame_width=2340, frame_height=1080)
        # No 'player' detection → use screen center heuristic.
        assert my_pos == (1170, 594)

    def test_my_pos_none_when_frame_unknown_and_no_player(self):
        raw = {"enemy": [[100, 100, 300, 300]]}
        my_pos, _ = parse_brawler_detections(raw)
        assert my_pos is None


class TestParsePowerCubes:
    def test_finds_power_cube_class(self):
        raw = {"power_cube": [[10, 10, 30, 30]], "wall": [[0, 0, 5, 5]]}
        cubes = parse_power_cubes(raw)
        assert len(cubes) == 1
        assert cubes[0].center == (20, 20)

    def test_case_insensitive(self):
        raw = {"PowerCube": [[10, 10, 30, 30]]}
        cubes = parse_power_cubes(raw)
        assert len(cubes) == 1


class TestBuildMatchState:
    def test_full_state(self):
        gs = build_match_state(
            frame_id=42,
            frame_width=1080,
            frame_height=1920,
            tile_raw={"wall": [[0, 0, 50, 50]], "bush": [[100, 100, 150, 150]]},
            brawler_raw={
                "player": [[400, 900, 700, 1200]],
                "enemy": [[50, 50, 250, 250], [800, 1700, 1000, 1900]],
            },
            main_raw={"power_cube": [[600, 600, 620, 620]]},
            my_brawler_class="colt",
        )
        assert gs.state == "match"
        assert gs.frame_id == 42
        assert gs.frame_width == 1080
        # my_pos comes from 'player' detection now.
        assert gs.my_pos == (550, 1050)
        assert len(gs.enemies) == 2
        assert len(gs.walls) == 1
        assert len(gs.bushes) == 1
        assert len(gs.power_cubes) == 1
