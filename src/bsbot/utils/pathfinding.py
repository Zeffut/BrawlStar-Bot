"""Grid-based A* pathfinding from `walls` bboxes to a target.

Used by ColtStrategy to navigate around walls instead of moving in a
straight line that would clip through them. Coarse cell size (~32-64 px)
keeps the grid small (~30×60 cells for a 1920×1080 frame) so A* runs in
well under 1 ms even when fully blocked.

Returned as a unit vector toward the *next waypoint*, so the strategy can
keep feeding the joystick worker. We don't bother returning the full path
because re-computing every few ticks (cheap) handles dynamic obstacles
(brawlers moving) for free.
"""
from __future__ import annotations

import heapq
import math
from dataclasses import dataclass
from typing import Iterable

from bsbot.utils.geometry import BBox


@dataclass(frozen=True)
class GridSpec:
    cell_size: int
    cols: int
    rows: int

    @classmethod
    def for_frame(cls, frame_width: int, frame_height: int, cell_size: int = 48) -> "GridSpec":
        return cls(
            cell_size=cell_size,
            cols=max(1, (frame_width + cell_size - 1) // cell_size),
            rows=max(1, (frame_height + cell_size - 1) // cell_size),
        )

    def cell_of(self, x: float, y: float) -> tuple[int, int]:
        cx = max(0, min(self.cols - 1, int(x // self.cell_size)))
        cy = max(0, min(self.rows - 1, int(y // self.cell_size)))
        return cx, cy

    def center_of(self, cx: int, cy: int) -> tuple[int, int]:
        return cx * self.cell_size + self.cell_size // 2, cy * self.cell_size + self.cell_size // 2


def build_blocked_grid(spec: GridSpec, walls: Iterable[BBox]) -> list[list[bool]]:
    """Returns a `cols × rows` grid of bool: True = blocked by a wall.

    A cell is blocked if any wall bbox overlaps more than a tiny corner.
    Cheap conservative test: bbox-cell intersection.
    """
    blocked = [[False] * spec.rows for _ in range(spec.cols)]
    cs = spec.cell_size
    frame_w = spec.cols * cs
    frame_h = spec.rows * cs
    for w in walls:
        # Skip walls fully outside the frame.
        if w.x2 < 0 or w.y2 < 0 or w.x1 >= frame_w or w.y1 >= frame_h:
            continue
        c0x, c0y = spec.cell_of(w.x1, w.y1)
        c1x, c1y = spec.cell_of(w.x2, w.y2)
        for cx in range(c0x, c1x + 1):
            if cx < 0 or cx >= spec.cols:
                continue
            for cy in range(c0y, c1y + 1):
                if cy < 0 or cy >= spec.rows:
                    continue
                blocked[cx][cy] = True
    return blocked


# 8-connectivity neighbours: (dx, dy, cost).
_NEIGHBOURS = (
    (1, 0, 1.0), (-1, 0, 1.0), (0, 1, 1.0), (0, -1, 1.0),
    (1, 1, math.sqrt(2)), (1, -1, math.sqrt(2)),
    (-1, 1, math.sqrt(2)), (-1, -1, math.sqrt(2)),
)


def astar(
    blocked: list[list[bool]],
    start: tuple[int, int],
    goal: tuple[int, int],
    *,
    max_nodes: int = 4000,
) -> list[tuple[int, int]] | None:
    """Return a path (list of cells, start→goal inclusive) or None if no path.

    Uses Manhattan-ish Euclidean heuristic. `max_nodes` is a hard cap to
    avoid runaway computation on very open maps.
    """
    if start == goal:
        return [start]
    cols, rows = len(blocked), len(blocked[0])
    sx, sy = start
    gx, gy = goal
    if not (0 <= sx < cols and 0 <= sy < rows and 0 <= gx < cols and 0 <= gy < rows):
        return None
    if blocked[gx][gy]:
        return None  # Can't go to a blocked cell.

    def h(x, y):
        return math.hypot(x - gx, y - gy)

    open_heap: list[tuple[float, int, tuple[int, int]]] = []
    counter = 0
    heapq.heappush(open_heap, (h(sx, sy), counter, (sx, sy)))
    came_from: dict[tuple[int, int], tuple[int, int]] = {}
    g_score: dict[tuple[int, int], float] = {(sx, sy): 0.0}
    explored = 0

    while open_heap:
        _f, _c, current = heapq.heappop(open_heap)
        if current == (gx, gy):
            # Reconstruct.
            path = [current]
            while current in came_from:
                current = came_from[current]
                path.append(current)
            path.reverse()
            return path
        explored += 1
        if explored > max_nodes:
            return None
        cx, cy = current
        for dx, dy, cost in _NEIGHBOURS:
            nx, ny = cx + dx, cy + dy
            if nx < 0 or nx >= cols or ny < 0 or ny >= rows:
                continue
            if blocked[nx][ny]:
                continue
            # Prevent diagonal "squeezing" through corner-adjacent walls.
            if dx != 0 and dy != 0:
                if blocked[cx + dx][cy] and blocked[cx][cy + dy]:
                    continue
            tentative = g_score[current] + cost
            if tentative < g_score.get((nx, ny), float("inf")):
                g_score[(nx, ny)] = tentative
                came_from[(nx, ny)] = current
                counter += 1
                heapq.heappush(open_heap, (tentative + h(nx, ny), counter, (nx, ny)))
    return None


def next_waypoint_vector(
    spec: GridSpec,
    walls: Iterable[BBox],
    from_pos: tuple[float, float],
    to_pos: tuple[float, float],
    lookahead_cells: int = 2,
) -> tuple[float, float] | None:
    """Plan a path and return a normalized vector toward the `lookahead_cells`-th cell.

    If no path is found, returns None. The caller can fallback to a direct
    vector or another strategy.
    """
    blocked = build_blocked_grid(spec, walls)
    start = spec.cell_of(*from_pos)
    goal = spec.cell_of(*to_pos)
    path = astar(blocked, start, goal)
    if path is None or len(path) < 2:
        return None
    target_cell = path[min(lookahead_cells, len(path) - 1)]
    tx, ty = spec.center_of(*target_cell)
    dx = tx - from_pos[0]
    dy = ty - from_pos[1]
    mag = math.hypot(dx, dy)
    if mag < 1e-6:
        return None
    return dx / mag, dy / mag
