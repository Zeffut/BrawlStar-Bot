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
    # Min bbox dimension to be considered a "real" on-field brawler.
    # Brawler portraits in UI corners (score indicators, party icons) are
    # ~70-90px tall; phantom detections from the model on tile patterns can
    # also be 100-140px. 150px threshold filters those out while still
    # keeping actual brawlers in landscape view (typically 200-400px).
    MIN_BRAWLER_SIZE_PX = 150
    # The camera centers on the player, so any detection within this radius
    # of my_pos is actually US (Brock) — exclude as a self-target.
    SELF_EXCLUSION_RADIUS_PX = 220

    my_pos: tuple[int, int] | None = None
    if frame_width > 0 and frame_height > 0:
        my_pos = (frame_width // 2, int(frame_height * 0.55))

    enemies: list[Enemy] = []
    for cls, boxes in raw.items():
        for xyxy in boxes:
            b = BBox.from_xyxy(xyxy)
            if min(b.w, b.h) < MIN_BRAWLER_SIZE_PX:
                continue
            if my_pos is not None:
                dx = b.cx - my_pos[0]
                dy = b.cy - my_pos[1]
                if (dx * dx + dy * dy) < (SELF_EXCLUSION_RADIUS_PX ** 2):
                    continue  # Skip self.
            enemies.append(Enemy(bbox=b, brawler_class=cls, confidence=1.0))

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
    my_pos, enemies = parse_brawler_detections(
        brawler_raw, my_brawler_class, frame_width=frame_width, frame_height=frame_height
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
