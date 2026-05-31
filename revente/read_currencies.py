"""Read gems / gold / level from a Brawl Stars lobby screenshot.

The parse helper is pure (unit-tested). The device-facing reader
(`read_currencies`) reuses the same OCR + crop pattern as
game_api._ocr_trophies and is exercised live (Phase 1, after BlueStacks
calibration).
"""
from __future__ import annotations

import re


def parse_currency_number(ocr_text: str) -> int | None:
    """Extract a currency integer from a noisy OCR string.

    Removes thousands separators (space/comma/dot between digits), then
    returns the longest digit run (tie -> largest), mirroring the
    trophy-OCR heuristic in game_api._ocr_trophies.
    """
    joined = re.sub(r"(?<=\d)[ ,.](?=\d)", "", ocr_text)
    runs = re.findall(r"\d+", joined)
    if not runs:
        return None
    return int(max(runs, key=lambda s: (len(s), int(s))))
