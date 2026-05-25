"""Tests for utils/geometry.py."""
from __future__ import annotations

import math

import pytest

from bsbot.utils.geometry import (
    BBox,
    angle_deg,
    clamp,
    distance,
    line_intersects_bbox,
    line_of_sight,
    normalize,
)


class TestBBox:
    def test_dimensions(self):
        b = BBox(10, 20, 50, 80)
        assert b.w == 40
        assert b.h == 60
        assert b.area == 2400
        assert b.center == (30, 50)

    def test_from_xyxy_swaps_inverted(self):
        b = BBox.from_xyxy([100, 100, 50, 80])
        assert b.x1 == 50 and b.x2 == 100
        assert b.y1 == 80 and b.y2 == 100

    def test_contains_point(self):
        b = BBox(0, 0, 10, 10)
        assert b.contains_point(5, 5)
        assert b.contains_point(0, 0)
        assert b.contains_point(10, 10)
        assert not b.contains_point(11, 5)
        assert not b.contains_point(-1, 5)

    def test_iou_self(self):
        b = BBox(0, 0, 10, 10)
        assert b.iou(b) == 1.0

    def test_iou_disjoint(self):
        a = BBox(0, 0, 10, 10)
        c = BBox(20, 20, 30, 30)
        assert a.iou(c) == 0.0

    def test_iou_partial(self):
        a = BBox(0, 0, 10, 10)
        c = BBox(5, 5, 15, 15)
        # intersection 5x5=25, union 100+100-25=175
        assert a.iou(c) == pytest.approx(25 / 175)


class TestDistance:
    def test_zero(self):
        assert distance((0, 0), (0, 0)) == 0

    def test_pythagoras(self):
        assert distance((0, 0), (3, 4)) == 5

    def test_negative(self):
        assert distance((-1, -1), (2, 3)) == pytest.approx(5)


class TestAngleDeg:
    def test_right(self):
        assert angle_deg((0, 0), (10, 0)) == 0

    def test_down(self):
        # +y axis points down in screen coordinates → 90°.
        assert angle_deg((0, 0), (0, 10)) == 90

    def test_left(self):
        assert abs(angle_deg((0, 0), (-10, 0))) == 180


class TestLineBBox:
    def test_segment_clearly_outside_misses(self):
        box = BBox(50, 50, 100, 100)
        assert not line_intersects_bbox((0, 0), (10, 10), box)

    def test_segment_passes_through(self):
        box = BBox(50, 50, 100, 100)
        assert line_intersects_bbox((0, 75), (200, 75), box)

    def test_segment_endpoint_inside(self):
        box = BBox(50, 50, 100, 100)
        assert line_intersects_bbox((0, 0), (75, 75), box)

    def test_segment_above_box_doesnt_intersect(self):
        box = BBox(0, 100, 200, 200)
        assert not line_intersects_bbox((0, 50), (200, 50), box)


class TestLineOfSight:
    def test_clear_path(self):
        obstacles = [BBox(0, 0, 10, 10)]
        assert line_of_sight((100, 100), (200, 100), obstacles)

    def test_wall_blocks(self):
        obstacles = [BBox(50, 0, 60, 200)]
        assert not line_of_sight((0, 100), (200, 100), obstacles)

    def test_path_around_wall(self):
        # Wall in the middle, path goes around (above) — actually a single straight
        # segment can't "go around", just check that a path passing tangent
        # doesn't trigger if it's clearly above.
        obstacles = [BBox(50, 100, 60, 200)]
        assert line_of_sight((0, 50), (200, 50), obstacles)

    def test_empty_obstacles_always_clear(self):
        assert line_of_sight((0, 0), (1000, 1000), [])


class TestUtils:
    def test_clamp(self):
        assert clamp(5, 0, 10) == 5
        assert clamp(-5, 0, 10) == 0
        assert clamp(15, 0, 10) == 10

    def test_normalize_zero(self):
        assert normalize((0, 0)) == (0.0, 0.0)

    def test_normalize_unit(self):
        x, y = normalize((3, 4))
        assert x == pytest.approx(0.6)
        assert y == pytest.approx(0.8)
        assert math.hypot(x, y) == pytest.approx(1.0)
