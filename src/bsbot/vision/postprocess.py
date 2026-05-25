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
    """Parse mainInGameModel detections (PylaAI-style).

    The model has classes ['enemy', 'teammate', 'player'] so we can:
    - Get my_pos directly from the 'player' detection (camera-locked sprite).
    - Use only 'enemy' detections as combat targets (skip teammates).

    Falls back to the old heuristic (largest detection near screen center
    = us) if no 'player' class detection is present.
    """
    # Fast path: mainInGameModel returns explicit 'player' and 'enemy' classes.
    my_pos: tuple[int, int] | None = None
    enemies: list[Enemy] = []

    player_boxes = raw.get("player", [])
    if player_boxes:
        # Pick the largest 'player' bbox (us, camera-focused).
        biggest = max(player_boxes, key=lambda xyxy: (xyxy[2] - xyxy[0]) * (xyxy[3] - xyxy[1]))
        b = BBox.from_xyxy(biggest)
        my_pos = b.center
    elif frame_width > 0 and frame_height > 0:
        # Fallback: assume camera center.
        my_pos = (frame_width // 2, int(frame_height * 0.55))

    for xyxy in raw.get("enemy", []):
        b = BBox.from_xyxy(xyxy)
        enemies.append(Enemy(bbox=b, brawler_class="enemy", confidence=1.0))
    # Teammates intentionally skipped — bot must not shoot them.

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
