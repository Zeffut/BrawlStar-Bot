"""Trophy OCR regression test.

Loads a real Mi 9T (2340x1080) lobby screenshot and verifies that
`_ocr_trophies` correctly reads the trophy count from the top-left
HUD (NOT the top-right season counter).

Requires `easyocr` to be installed — skipped otherwise (e.g. local
dev machine). Runs on the bot host where the full stack is installed.
"""
from __future__ import annotations

from pathlib import Path

import pytest

easyocr = pytest.importorskip("easyocr")
np = pytest.importorskip("numpy")
pil = pytest.importorskip("PIL.Image")

from PIL import Image

import game_api  # noqa: E402

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(scope="module")
def lobby_arr():
    img = Image.open(FIXTURES / "lobby_mi9t_2340x1080.png").convert("RGB")
    return np.array(img)


def test_lobby_resolution(lobby_arr):
    h, w, _ = lobby_arr.shape
    assert (w, h) == (2340, 1080), f"unexpected fixture size: {w}x{h}"


def test_trophies_read_from_top_left(lobby_arr):
    """The fixture is from an account at ~630 trophies."""
    val = game_api._ocr_trophies(lobby_arr)
    assert val == 630, f"expected 630, got {val}"


def test_trophies_not_picking_wrong_value(lobby_arr):
    """Regression: the OCR must NOT return season counter (647), coins (3007),
    gems (10) or the equipped brawler trophies (369) — only the top-left
    trophy pill next to the avatar (~630)."""
    val = game_api._ocr_trophies(lobby_arr)
    assert val is not None
    forbidden = {3007, 10, 647, 369}
    assert val not in forbidden, f"OCR picked wrong number {val} from forbidden set"
    assert 100 < val < 10000, f"unrealistic trophy value: {val}"


def test_trophies_returns_none_on_unrelated_image():
    """A blank black image should yield no trophy reading."""
    blank = np.zeros((1080, 2340, 3), dtype=np.uint8)
    assert game_api._ocr_trophies(blank) is None
