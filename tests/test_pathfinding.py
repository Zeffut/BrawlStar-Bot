"""Tests for utils/pathfinding.py."""
from __future__ import annotations

import math

import pytest

from bsbot.utils.geometry import BBox
from bsbot.utils.pathfinding import (
    GridSpec,
    astar,
    build_blocked_grid,
    next_waypoint_vector,
)


class TestGridSpec:
    def test_for_frame_rounds_up(self):
        spec = GridSpec.for_frame(1920, 1080, cell_size=64)
        assert spec.cols == 30
        assert spec.rows == 17  # 1080/64 = 16.875 → 17

    def test_cell_of_clamps(self):
        spec = GridSpec.for_frame(1920, 1080, cell_size=64)
        assert spec.cell_of(-100, -100) == (0, 0)
        assert spec.cell_of(99999, 99999) == (spec.cols - 1, spec.rows - 1)

    def test_center_of_known(self):
        spec = GridSpec(cell_size=10, cols=10, rows=10)
        assert spec.center_of(0, 0) == (5, 5)
        assert spec.center_of(3, 4) == (35, 45)


class TestBuildBlockedGrid:
    def test_empty_walls(self):
        spec = GridSpec(cell_size=10, cols=10, rows=10)
        grid = build_blocked_grid(spec, [])
        assert all(not cell for col in grid for cell in col)

    def test_single_wall_marks_overlapping_cells(self):
        spec = GridSpec(cell_size=10, cols=10, rows=10)
        # Wall spans cells (2,2) → (4,4) inclusive.
        wall = BBox(25, 25, 45, 45)
        grid = build_blocked_grid(spec, [wall])
        for cx in range(2, 5):
            for cy in range(2, 5):
                assert grid[cx][cy], f"cell ({cx},{cy}) should be blocked"
        # Cells around should be free.
        assert not grid[0][0]
        assert not grid[5][5]

    def test_wall_outside_frame_ignored(self):
        spec = GridSpec(cell_size=10, cols=10, rows=10)
        wall = BBox(1000, 1000, 2000, 2000)
        grid = build_blocked_grid(spec, [wall])
        assert all(not cell for col in grid for cell in col)


class TestAStar:
    def test_same_cell(self):
        blocked = [[False] * 5 for _ in range(5)]
        assert astar(blocked, (2, 2), (2, 2)) == [(2, 2)]

    def test_straight_path_open_grid(self):
        blocked = [[False] * 5 for _ in range(5)]
        path = astar(blocked, (0, 0), (4, 0))
        assert path is not None
        assert path[0] == (0, 0)
        assert path[-1] == (4, 0)
        # On an open grid, A* should produce a roughly straight path.
        assert len(path) == 5

    def test_path_avoids_wall(self):
        blocked = [[False] * 5 for _ in range(5)]
        # Wall column at x=2, y=0..3 (leaves x=2,y=4 open).
        for y in range(0, 4):
            blocked[2][y] = True
        path = astar(blocked, (0, 0), (4, 0))
        assert path is not None
        # The path must go around the wall (through (2,4) or further).
        ys_on_path = [c[1] for c in path]
        assert max(ys_on_path) >= 4

    def test_no_path_completely_blocked(self):
        blocked = [[False] * 5 for _ in range(5)]
        for y in range(5):
            blocked[2][y] = True
        # Goal on the other side of a full wall column → no path.
        path = astar(blocked, (0, 0), (4, 0))
        assert path is None

    def test_goal_on_blocked_cell(self):
        blocked = [[False] * 5 for _ in range(5)]
        blocked[3][3] = True
        path = astar(blocked, (0, 0), (3, 3))
        assert path is None

    def test_diagonal_squeeze_prevented(self):
        """A* must not cut through the corner between two diagonal walls."""
        blocked = [[False] * 5 for _ in range(5)]
        # Two walls forming an "X" corner at (1,1) and (2,2):
        blocked[1][2] = True
        blocked[2][1] = True
        path = astar(blocked, (1, 1), (2, 2))
        # The direct diagonal step would "squeeze" between the two walls,
        # which our code prevents. So path must detour or be None.
        # In a 5x5 with only these 2 walls there's a detour: (1,1)→(0,1)→(0,2)→(1,2)? no, blocked.
        # Let's just assert that *if* a path exists, it has more than 2 cells.
        if path is not None:
            assert len(path) > 2

    def test_max_nodes_cap(self):
        """On a huge grid, A* should bail out gracefully."""
        # Open 100x100 grid, but very tight cap.
        blocked = [[False] * 100 for _ in range(100)]
        path = astar(blocked, (0, 0), (99, 99), max_nodes=10)
        # Either returns a path (lucky) or None — must not crash.
        assert path is None or path[0] == (0, 0)


class TestNextWaypointVector:
    def test_open_field_returns_direct_vector(self):
        spec = GridSpec.for_frame(1000, 1000, cell_size=50)
        v = next_waypoint_vector(spec, walls=[], from_pos=(100, 500), to_pos=(900, 500))
        assert v is not None
        ux, uy = v
        # Should head mostly +x.
        assert ux > 0.7
        assert abs(uy) < 0.3
        assert abs(math.hypot(ux, uy) - 1.0) < 1e-6

    def test_with_wall_detours(self):
        spec = GridSpec.for_frame(1000, 1000, cell_size=50)
        # Vertical wall at x=400..450, y=200..800.
        walls = [BBox(400, 200, 450, 800)]
        v = next_waypoint_vector(spec, walls=walls, from_pos=(100, 500), to_pos=(900, 500))
        assert v is not None
        # The vector should NOT be straight +x — it should bend.
        ux, uy = v
        assert abs(uy) > 0.1, f"expected detour, got vector {v}"

    def test_same_cell_returns_none(self):
        spec = GridSpec.for_frame(500, 500, cell_size=50)
        v = next_waypoint_vector(spec, walls=[], from_pos=(110, 110), to_pos=(140, 140))
        # Both points fall in cell (2,2) → path is just [(2,2)] → returns None.
        assert v is None

    def test_no_path_returns_none(self):
        spec = GridSpec.for_frame(500, 500, cell_size=50)
        # Full wall column blocking from x=200 to x=300.
        walls = [BBox(200, 0, 300, 500)]
        v = next_waypoint_vector(spec, walls=walls, from_pos=(50, 250), to_pos=(450, 250))
        # Should be None because wall fully blocks.
        # Note: build_blocked_grid is conservative — wall at x=200..300 with
        # cell_size=50 blocks cells (4,*) and (5,*) and possibly (6,*).
        # So blocking is complete; no path.
        assert v is None
