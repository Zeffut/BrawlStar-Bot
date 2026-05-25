"""Tests for vision/state_finder.py — synthetic templates and frames."""
from __future__ import annotations

from pathlib import Path

import cv2
import numpy as np
import pytest

from bsbot.vision.state_finder import StateFinder


@pytest.fixture
def empty_dir(tmp_path: Path) -> Path:
    return tmp_path


def _distinctive_template(seed: int, size: int = 40) -> np.ndarray:
    """A template with a unique random pattern (high variance — needed for
    TM_CCOEFF_NORMED to discriminate)."""
    rng = np.random.default_rng(seed)
    return rng.integers(0, 255, size=(size, size), dtype=np.uint8)


@pytest.fixture
def populated_dir(tmp_path: Path) -> Path:
    """Build a fake templates dir with one distinctive pattern per state."""
    for state, seed in [("lobby", 1), ("match", 2), ("end", 3)]:
        d = tmp_path / state
        d.mkdir()
        img = _distinctive_template(seed)
        cv2.imwrite(str(d / f"{state}.png"), img)
    return tmp_path


def _make_frame_with(template: np.ndarray, pos: tuple[int, int] = (200, 300)) -> np.ndarray:
    """Paste a template into a uniform-grey frame at `pos`. Returns BGR frame.

    Uniform background avoids accidental high correlation with other templates.
    """
    frame = np.full((480, 640), 128, dtype=np.uint8)
    x, y = pos
    h, w = template.shape
    frame[y:y+h, x:x+w] = template
    return cv2.cvtColor(frame, cv2.COLOR_GRAY2BGR)


class TestStateFinderEmpty:
    def test_no_templates_returns_none(self, empty_dir):
        finder = StateFinder(empty_dir)
        frame = np.zeros((480, 640, 3), dtype=np.uint8)
        assert finder.detect(frame) is None
        assert finder.detect_state_name(frame) == "unknown"


class TestStateFinderMatching:
    def test_loads_templates(self, populated_dir):
        finder = StateFinder(populated_dir)
        assert finder.n_templates == 3

    def test_detects_lobby(self, populated_dir):
        finder = StateFinder(populated_dir, threshold=0.7)
        lobby_img = cv2.imread(str(populated_dir / "lobby" / "lobby.png"), cv2.IMREAD_GRAYSCALE)
        frame = _make_frame_with(lobby_img, pos=(100, 100))
        match = finder.detect(frame)
        assert match is not None
        assert match.state == "lobby"
        assert match.score >= 0.7

    def test_detects_match(self, populated_dir):
        finder = StateFinder(populated_dir, threshold=0.7)
        match_img = cv2.imread(str(populated_dir / "match" / "match.png"), cv2.IMREAD_GRAYSCALE)
        frame = _make_frame_with(match_img)
        m = finder.detect(frame)
        assert m is not None
        assert m.state == "match"

    def test_returns_none_for_unrelated_frame(self, populated_dir):
        finder = StateFinder(populated_dir, threshold=0.95)
        # Pure random frame with no embedded template → should not match.
        rng = np.random.default_rng(0)
        frame = rng.integers(0, 255, size=(480, 640, 3), dtype=np.uint8)
        assert finder.detect(frame) is None

    def test_reload_picks_up_new_template(self, populated_dir):
        finder = StateFinder(populated_dir, threshold=0.7)
        assert finder.n_templates == 3
        # Add a new template at runtime.
        new_dir = populated_dir / "popup"
        new_dir.mkdir()
        img = _distinctive_template(seed=99)
        cv2.imwrite(str(new_dir / "x.png"), img)
        finder.reload()
        assert finder.n_templates == 4
