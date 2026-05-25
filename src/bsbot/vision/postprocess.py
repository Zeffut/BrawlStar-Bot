"""Parse raw ONNX detections into a structured GameState consumed by strategies.

Each model returns `{class_name: [[x1,y1,x2,y2], ...]}` via `Detect.detect_objects`.
We merge results from multiple models into one GameState.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Literal

from bsbot.utils.geometry import BBox


GameStateName = Literal[
    "lobby",
    "match",
    "end",
    "popup",
    "starting",
    "disconnect",
    "unknown",
]


@dataclass
class Enemy:
    bbox: BBox
    brawler_class: str
    confidence: float

    @property
    def position(self) -> tuple[int, int]:
        return self.bbox.center


@dataclass
class GameState:
    state: GameStateName
    frame_id: int = 0
    timestamp: float = field(default_factory=time.time)

    # Match-only fields. None if not in match.
    my_pos: tuple[int, int] | None = None
    enemies: list[Enemy] = field(default_factory=list)
    walls: list[BBox] = field(default_factory=list)
    bushes: list[BBox] = field(default_factory=list)
    power_cubes: list[BBox] = field(default_factory=list)

    # UI hints — filled when in match if we manage to read them.
    my_health_pct: float | None = None
    my_super_charge_pct: float | None = None
    my_gadget_available: bool | None = None

    # Frame metadata — useful for strategies to compute relative positions.
    frame_width: int = 0
    frame_height: int = 0


# --- parsing helpers -----------------------------------------------------


def parse_tile_detections(raw: dict[str, list[list[int]]]) -> tuple[list[BBox], list[BBox]]:
    """Split `tileDetector` output into walls (blocking) and bushes (cover).

    Class names per `bot_config.toml.wall_model_classes = ["wall","bush","close_bush"]`.
    """
    walls: list[BBox] = []
    bushes: list[BBox] = []
    for cls, boxes in raw.items():
        for xyxy in boxes:
            b = BBox.from_xyxy(xyxy)
            if cls == "wall":
                walls.append(b)
            elif cls in ("bush", "close_bush"):
                bushes.append(b)
    return walls, bushes


def parse_brawler_detections(
    raw: dict[str, list[list[int]]],
    my_brawler_class: str | None = None,
    frame_width: int = 0,
    frame_height: int = 0,
) -> tuple[tuple[int, int] | None, list[Enemy]]:
    """Split `brawlersInGame` output into (my_pos, enemies).

    Brawl Stars centers the camera on the local player, so the player's
    sprite is always approximately at the middle of the frame (slightly
    below center). We exploit that:

    - **my_pos = (frame_width/2, frame_height * 0.55)** — assumed fixed.
    - Every detection from the model is treated as an enemy/teammate.
      We can't reliably tell teammates from enemies without UI hints
      (allies have a blue circle, enemies a red one) so for v1 every
      detection is considered hostile.
    """
    MIN_BRAWLER_SIZE_PX = 150
    # Real brawlers in landscape are roughly 200-400px wide; phantom hits on
    # tile patterns can sometimes be 500-800px wide. Cap to filter those out.
    MAX_BRAWLER_SIZE_PX = 450
    # Brawler sprite is taller than wide (humanoid). Aspect ratio h/w between
    # 0.8 and 2.0 covers all real shapes; values outside are likely phantoms.
    MIN_ASPECT_RATIO_H_OVER_W = 0.7
    MAX_ASPECT_RATIO_H_OVER_W = 2.5
    # The camera doesn't always center perfectly on the player (offset when
    # moving). Within this search radius around screen center, the LARGEST
    # detection is treated as us and excluded.
    SELF_SEARCH_RADIUS_PX = 350

    my_pos: tuple[int, int] | None = None
    if frame_width > 0 and frame_height > 0:
        my_pos = (frame_width // 2, int(frame_height * 0.55))

    # Collect all valid (large enough) brawler detections.
    valid: list[tuple[str, BBox]] = []
    for cls, boxes in raw.items():
        for xyxy in boxes:
            b = BBox.from_xyxy(xyxy)
            if min(b.w, b.h) < MIN_BRAWLER_SIZE_PX:
                continue
            if max(b.w, b.h) > MAX_BRAWLER_SIZE_PX:
                continue  # too big, probably a wall/floor cluster
            ratio = b.h / max(b.w, 1)
            if ratio < MIN_ASPECT_RATIO_H_OVER_W or ratio > MAX_ASPECT_RATIO_H_OVER_W:
                continue
            valid.append((cls, b))

    # Identify "ourself": detection closest to my_pos within SELF_SEARCH_RADIUS.
    # Closer wins (camera centers on player, so we should be nearest to
    # screen center even if an enemy passes by with a bigger bbox).
    self_idx: int | None = None
    if my_pos is not None and valid:
        best_dist2: float | None = None
        for i, (_cls, b) in enumerate(valid):
            dx = b.cx - my_pos[0]
            dy = b.cy - my_pos[1]
            d2 = dx * dx + dy * dy
            if d2 <= (SELF_SEARCH_RADIUS_PX ** 2) and (best_dist2 is None or d2 < best_dist2):
                best_dist2 = d2
                self_idx = i

    enemies: list[Enemy] = [
        Enemy(bbox=b, brawler_class=cls, confidence=1.0)
        for i, (cls, b) in enumerate(valid)
        if i != self_idx
    ]
    return my_pos, enemies


def parse_power_cubes(raw: dict[str, list[list[int]]]) -> list[BBox]:
    """`mainInGameModel` should output a 'power_cube' class. Returns its bboxes."""
    boxes: list[BBox] = []
    for cls, raw_boxes in raw.items():
        if "cube" in cls.lower() or "power" in cls.lower():
            for xyxy in raw_boxes:
                boxes.append(BBox.from_xyxy(xyxy))
    return boxes


def build_match_state(
    *,
    frame_id: int,
    frame_width: int,
    frame_height: int,
    tile_raw: dict[str, list[list[int]]],
    brawler_raw: dict[str, list[list[int]]],
    main_raw: dict[str, list[list[int]]] | None = None,
    my_brawler_class: str | None = None,
) -> GameState:
    """Convenience constructor: turn all detector outputs into one GameState(match)."""
    walls, bushes = parse_tile_detections(tile_raw)
    # Filter brawler detections that heavily overlap walls/bushes — those
    # are phantom hits where the model misfired on tile patterns.
    obstacles = walls + bushes
    filtered_brawler_raw: dict[str, list[list[int]]] = {}
    for cls, boxes in brawler_raw.items():
        kept: list[list[int]] = []
        for xyxy in boxes:
            b = BBox.from_xyxy(xyxy)
            # If >40% of this bbox area overlaps a wall/bush, drop it.
            max_iou = 0.0
            for obs in obstacles:
                if b.intersects(obs):
                    ix1 = max(b.x1, obs.x1)
                    iy1 = max(b.y1, obs.y1)
                    ix2 = min(b.x2, obs.x2)
                    iy2 = min(b.y2, obs.y2)
                    inter = max(0, ix2 - ix1) * max(0, iy2 - iy1)
                    if b.area > 0:
                        ratio = inter / b.area
                        if ratio > max_iou:
                            max_iou = ratio
            if max_iou <= 0.4:
                kept.append(xyxy)
        if kept:
            filtered_brawler_raw[cls] = kept

    my_pos, enemies = parse_brawler_detections(
        filtered_brawler_raw, my_brawler_class,
        frame_width=frame_width, frame_height=frame_height,
    )
    cubes = parse_power_cubes(main_raw or {})
    return GameState(
        state="match",
        frame_id=frame_id,
        timestamp=time.time(),
        my_pos=my_pos,
        enemies=enemies,
        walls=walls,
        bushes=bushes,
        power_cubes=cubes,
        frame_width=frame_width,
        frame_height=frame_height,
    )
