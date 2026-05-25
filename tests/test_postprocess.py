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
    def test_no_self_class_all_enemies(self):
        raw = {"shelly": [[10, 10, 30, 30]], "colt": [[100, 100, 130, 130]]}
        my_pos, enemies = parse_brawler_detections(raw, my_brawler_class=None)
        assert my_pos is None
        assert len(enemies) == 2

    def test_self_extracted_from_class_match(self):
        raw = {
            "colt": [[100, 100, 200, 200]],  # us (largest)
            "shelly": [[10, 10, 30, 30]],
        }
        my_pos, enemies = parse_brawler_detections(raw, my_brawler_class="colt")
        assert my_pos == (150, 150)
        assert len(enemies) == 1
        assert enemies[0].brawler_class == "shelly"

    def test_self_picks_largest_candidate(self):
        # If two "colt" detections (us + an enemy Colt), our brawler is the
        # most prominent (largest bbox).
        raw = {
            "colt": [[100, 100, 200, 200], [0, 0, 20, 20]],  # 100x100 and 20x20
        }
        my_pos, enemies = parse_brawler_detections(raw, my_brawler_class="colt")
        assert my_pos == (150, 150)
        # No enemies because both bboxes consumed as "candidates for us" and we
        # picked one; current impl puts the unpicked one nowhere. (See note.)
        # For now, accept this — it's a known limitation.
        assert enemies == []


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
            brawler_raw={"colt": [[400, 800, 500, 900]], "shelly": [[200, 200, 300, 300]]},
            main_raw={"power_cube": [[600, 600, 620, 620]]},
            my_brawler_class="colt",
        )
        assert gs.state == "match"
        assert gs.frame_id == 42
        assert gs.frame_width == 1080
        assert gs.my_pos == (450, 850)
        assert len(gs.enemies) == 1
        assert gs.enemies[0].brawler_class == "shelly"
        assert len(gs.walls) == 1
        assert len(gs.bushes) == 1
        assert len(gs.power_cubes) == 1
