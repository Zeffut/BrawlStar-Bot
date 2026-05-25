"""State-finder regression test.

For each labeled fixture under `tests/fixtures/states/<state>/`, verify the
StateFinder returns the expected state. Fails the build if any fixture
no longer detects correctly — a tripwire for template tweaks.

Templates that are still vacant (no fixture for the state) are skipped, not
failed — fixtures are collected over time via `tools/fixture_collect.py`.
"""
from __future__ import annotations

from pathlib import Path

import cv2
import pytest

from bsbot.vision.state_finder import StateFinder

REPO_ROOT = Path(__file__).resolve().parent.parent
FIXTURES_DIR = REPO_ROOT / "tests" / "fixtures" / "states"
TEMPLATES_DIR = REPO_ROOT / "src" / "bsbot" / "data" / "state_templates"


def _collect_fixtures():
    """Yield (state, path) for every PNG under fixtures/states/<state>/."""
    if not FIXTURES_DIR.exists():
        return
    for state_dir in sorted(FIXTURES_DIR.iterdir()):
        if not state_dir.is_dir():
            continue
        state = state_dir.name
        for img_path in sorted(state_dir.glob("*.png")):
            yield state, img_path


FIXTURES = list(_collect_fixtures())


@pytest.fixture(scope="module")
def state_finder() -> StateFinder:
    return StateFinder(TEMPLATES_DIR, threshold=0.85)


@pytest.mark.skipif(not FIXTURES, reason="no fixtures collected yet")
@pytest.mark.parametrize("expected_state,fixture_path", FIXTURES,
                         ids=lambda x: x.name if hasattr(x, "name") else str(x))
def test_state_detected(expected_state, fixture_path, state_finder):
    frame = cv2.imread(str(fixture_path))
    assert frame is not None, f"could not read {fixture_path}"
    match = state_finder.detect(frame)
    if expected_state == "unknown":
        # Negative fixture — we should NOT detect anything.
        assert match is None, (
            f"{fixture_path.name}: expected no detection but got "
            f"{match.state if match else None} (score={match.score if match else 0})"
        )
        return
    assert match is not None, f"{fixture_path.name}: state_finder returned None"
    assert match.state == expected_state, (
        f"{fixture_path.name}: expected {expected_state} but got "
        f"{match.state} (template={match.template_name}, score={match.score:.3f})"
    )


def test_at_least_one_fixture_per_known_state():
    """Track which states still need fixtures (informational, not fatal)."""
    seen_states = {state for state, _ in FIXTURES}
    expected = {"lobby", "match", "end", "popup", "starting", "disconnect"}
    missing = expected - seen_states
    if missing:
        pytest.skip(f"States with no fixtures yet: {sorted(missing)}")
