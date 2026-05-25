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
    def test_all_detections_become_enemies(self):
        """Camera-centered assumption: my_pos is always screen center;
        large detections far from center become enemies."""
        # Both bboxes are far from center (500, 550) so neither is excluded.
        raw = {"shelly": [[10, 10, 200, 200]], "colt": [[800, 800, 999, 999]]}
        my_pos, enemies = parse_brawler_detections(
            raw, my_brawler_class=None, frame_width=1000, frame_height=1000
        )
        assert my_pos == (500, 550)
        assert len(enemies) == 2

    def test_self_detection_at_center_excluded(self):
        """Detection within SELF_EXCLUSION_RADIUS_PX of my_pos = ourself."""
        # Center at (500, 550). This detection is centered at (510, 560) — that's us.
        raw = {"brock": [[450, 510, 570, 630]]}
        _my_pos, enemies = parse_brawler_detections(
            raw, frame_width=1000, frame_height=1000
        )
        assert enemies == []

    def test_small_ui_detections_filtered_out(self):
        """Tiny brawler bboxes (e.g. score icons in UI corners) are ignored."""
        raw = {"shelly": [[10, 10, 80, 80]]}  # 70x70 — too small
        _, enemies = parse_brawler_detections(
            raw, frame_width=1000, frame_height=1000
        )
        assert enemies == []

    def test_my_pos_none_when_frame_unknown(self):
        raw = {"colt": [[100, 100, 300, 300]]}  # 200x200 — big enough
        my_pos, enemies = parse_brawler_detections(raw)
        assert my_pos is None  # no frame size → can't compute center
        assert len(enemies) == 1

    def test_my_pos_at_landscape_center(self):
        my_pos, _ = parse_brawler_detections(
            {}, frame_width=2340, frame_height=1080
        )
        assert my_pos == (1170, 594)  # 2340/2, 1080*0.55 = 594


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
            brawler_raw={"colt": [[50, 50, 250, 250]], "shelly": [[800, 1700, 1000, 1900]]},
            main_raw={"power_cube": [[600, 600, 620, 620]]},
            my_brawler_class="colt",
        )
        assert gs.state == "match"
        assert gs.frame_id == 42
        assert gs.frame_width == 1080
        # my_pos = screen center (camera follows player).
        assert gs.my_pos == (540, 1056)  # 1080/2, 1920*0.55
        # All brawler detections are enemies now.
        assert len(gs.enemies) == 2
        assert len(gs.walls) == 1
        assert len(gs.bushes) == 1
        assert len(gs.power_cubes) == 1
