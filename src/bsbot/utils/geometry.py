"""Pure-math helpers: bbox, distance, angle, line-of-sight."""
from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass(frozen=True)
class BBox:
    """Axis-aligned bounding box in pixel coordinates (x1 < x2, y1 < y2)."""

    x1: int
    y1: int
    x2: int
    y2: int

    @property
    def w(self) -> int:
        return self.x2 - self.x1

    @property
    def h(self) -> int:
        return self.y2 - self.y1

    @property
    def cx(self) -> int:
        return (self.x1 + self.x2) // 2

    @property
    def cy(self) -> int:
        return (self.y1 + self.y2) // 2

    @property
    def center(self) -> tuple[int, int]:
        return self.cx, self.cy

    @property
    def area(self) -> int:
        return self.w * self.h

    def contains_point(self, x: float, y: float) -> bool:
        return self.x1 <= x <= self.x2 and self.y1 <= y <= self.y2

    def intersects(self, other: "BBox") -> bool:
        return not (
            self.x2 < other.x1 or other.x2 < self.x1 or self.y2 < other.y1 or other.y2 < self.y1
        )

    def iou(self, other: "BBox") -> float:
        if not self.intersects(other):
            return 0.0
        ix1 = max(self.x1, other.x1)
        iy1 = max(self.y1, other.y1)
        ix2 = min(self.x2, other.x2)
        iy2 = min(self.y2, other.y2)
        inter = max(0, ix2 - ix1) * max(0, iy2 - iy1)
        union = self.area + other.area - inter
        return inter / union if union else 0.0

    @classmethod
    def from_xyxy(cls, xyxy) -> "BBox":
        x1, y1, x2, y2 = (int(v) for v in xyxy)
        if x1 > x2:
            x1, x2 = x2, x1
        if y1 > y2:
            y1, y2 = y2, y1
        return cls(x1, y1, x2, y2)


def distance(a: tuple[float, float], b: tuple[float, float]) -> float:
    return math.hypot(a[0] - b[0], a[1] - b[1])


def angle_deg(origin: tuple[float, float], target: tuple[float, float]) -> float:
    """Angle from `origin` to `target` in degrees, 0° = +x axis, 90° = -y axis (screen up)."""
    dx = target[0] - origin[0]
    dy = target[1] - origin[1]
    return math.degrees(math.atan2(dy, dx))


def line_intersects_bbox(p1: tuple[float, float], p2: tuple[float, float], box: BBox) -> bool:
    """Liang-Barsky line-clipping: does the segment p1->p2 intersect `box`?"""
    x1, y1 = p1
    x2, y2 = p2
    dx, dy = x2 - x1, y2 - y1
    p = [-dx, dx, -dy, dy]
    q = [x1 - box.x1, box.x2 - x1, y1 - box.y1, box.y2 - y1]
    u1, u2 = 0.0, 1.0
    for pi, qi in zip(p, q):
        if pi == 0:
            if qi < 0:
                return False  # parallel and outside
            continue
        t = qi / pi
        if pi < 0:
            if t > u2:
                return False
            if t > u1:
                u1 = t
        else:
            if t < u1:
                return False
            if t < u2:
                u2 = t
    return u1 <= u2


def line_of_sight(
    p1: tuple[float, float],
    p2: tuple[float, float],
    obstacles: list[BBox],
) -> bool:
    """Return True if no obstacle bbox blocks the segment p1->p2."""
    for box in obstacles:
        if line_intersects_bbox(p1, p2, box):
            return False
    return True


def clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def normalize(vec: tuple[float, float]) -> tuple[float, float]:
    mag = math.hypot(*vec)
    if mag < 1e-9:
        return 0.0, 0.0
    return vec[0] / mag, vec[1] / mag
