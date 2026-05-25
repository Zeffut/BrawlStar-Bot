"""End-to-end smoke test — wire VisionWorker + BrainWorker + ControlWorker
together with a scripted frame source and a fake ADB device. Verifies the
whole pipeline drives correct actions for a sequence of states.

This is the canary for full-pipeline regressions: if state detection,
postprocessing, strategy dispatch, or action translation breaks, this
test should catch it.
"""
from __future__ import annotations

import threading
import time
from pathlib import Path

import cv2
import pytest

from bsbot.buses import ControlBus, LatestSlot
from bsbot.controls.inputs import ActionType
from bsbot.strategies.colt import BrawlerStats, ColtStrategy, ColtTuning
from bsbot.strategies.menu import MenuCoords, MenuStrategy
from bsbot.vision.postprocess import GameState
from bsbot.workers.brain import BrainWorker
from bsbot.workers.control import ButtonLayout, ControlWorker
from bsbot.workers.vision import VisionWorker
from tests.mocks import FakeAdbDevice, ScriptedFrameSource, drain_actions, make_fake_adb

REPO_ROOT = Path(__file__).resolve().parent.parent
MODELS_DIR = REPO_ROOT / "src" / "bsbot" / "models"
TEMPLATES_DIR = REPO_ROOT / "src" / "bsbot" / "data" / "state_templates"
FIXTURES_DIR = REPO_ROOT / "tests" / "fixtures" / "states"


def _load_fixture(state: str) -> "np.ndarray":
    import numpy as np  # noqa: F401
    pngs = list((FIXTURES_DIR / state).glob("*.png"))
    if not pngs:
        pytest.skip(f"no fixture for state '{state}'")
    return cv2.imread(str(pngs[0]))


@pytest.fixture(scope="module")
def colt_stats():
    return BrawlerStats.load(REPO_ROOT / "src" / "bsbot" / "data" / "brawlers_info.json", "brock")


@pytest.fixture
def workers_and_buses(colt_stats):
    """Spin up the whole pipeline with fake ADB. Yields (vision, brain, control,
    frame_slot, state_slot, control_bus, fake_device, stop_event)."""
    frame_slot: LatestSlot = LatestSlot()
    state_slot: LatestSlot[GameState] = LatestSlot()
    control_bus = ControlBus()
    stop_event = threading.Event()

    adb = make_fake_adb()
    strategies = {
        "match": ColtStrategy(stats=colt_stats, tuning=ColtTuning(drift_toward_center=False),
                              min_action_interval_s=0.0),
        "lobby": MenuStrategy(MenuCoords(), action_cooldown_s=0.1),
        "end": MenuStrategy(MenuCoords(), action_cooldown_s=0.1),
        "popup": MenuStrategy(MenuCoords(), action_cooldown_s=0.1),
        "disconnect": MenuStrategy(MenuCoords(), action_cooldown_s=0.1),
        "starting": MenuStrategy(MenuCoords(), action_cooldown_s=0.1),
    }

    vision = VisionWorker(frame_slot, state_slot, stop_event,
                          models_dir=MODELS_DIR, templates_dir=TEMPLATES_DIR,
                          my_brawler_class="brock", preferred_device="cpu")
    brain = BrainWorker(state_slot, control_bus, stop_event, strategies=strategies,
                        tick_timeout_s=0.1)
    control = ControlWorker(control_bus, adb, stop_event,
                            layout=ButtonLayout(), joystick_tick_ms=50)

    vision.start()
    brain.start()
    control.start()

    yield (vision, brain, control, frame_slot, state_slot, control_bus, adb, stop_event)

    stop_event.set()
    for w in (vision, brain, control):
        w.join(timeout=3.0)


def _wait_for_action(bus: ControlBus, action_type: ActionType, timeout_s: float = 5.0,
                     collected: list | None = None):
    """Wait for any action of the given type. If `collected` is provided,
    every observed action (including non-matching) is appended for debug.
    """
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            action = bus.get(timeout=0.1)
            if collected is not None:
                collected.append(action)
            if action.type == action_type:
                return action
        except Exception:
            pass
    return None


@pytest.mark.slow
def test_lobby_fixture_triggers_play_tap(workers_and_buses):
    """Inject a lobby screenshot → bot should TAP the JOUER button."""
    vision, brain, control, frame_slot, state_slot, control_bus, adb, stop_event = workers_and_buses

    frame = _load_fixture("lobby")
    frame_slot.set(frame)

    action = _wait_for_action(control_bus, ActionType.TAP, timeout_s=8.0)
    assert action is not None, "expected a TAP action within 8s"
    # play_button = MenuCoords.play_button (current calibration)
    from bsbot.strategies.menu import MenuCoords
    assert (action.x, action.y) == MenuCoords().play_button, (
        f"expected play button tap, got {action}"
    )


@pytest.mark.slow
def test_disconnect_fixture_triggers_reconnect_tap(workers_and_buses):
    """Inject AFK disconnect screen → bot should TAP RECHARGER (545, 728)."""
    vision, brain, control, frame_slot, state_slot, control_bus, adb, stop_event = workers_and_buses

    frame = _load_fixture("disconnect")
    frame_slot.set(frame)

    action = _wait_for_action(control_bus, ActionType.TAP, timeout_s=8.0)
    assert action is not None, "expected a TAP action"
    from bsbot.strategies.menu import MenuCoords
    assert (action.x, action.y) == MenuCoords().reconnect_button, (
        f"expected reconnect tap, got {action}"
    )


@pytest.mark.slow
def test_end_fixture_triggers_dismiss_tap(workers_and_buses):
    """Inject post-match screen → bot should TAP a known dismiss position.

    End-screen layouts vary (VICTOIRE has CONTINUER; DÉFAITE has
    REJOUER+QUITTER) so we only assert the tap is in the bottom-right
    button strip (y ~1000, x in 1900-2300).
    """
    vision, brain, control, frame_slot, state_slot, control_bus, adb, stop_event = workers_and_buses

    frame = _load_fixture("end")
    frame_slot.set(frame)

    action = _wait_for_action(control_bus, ActionType.TAP, timeout_s=8.0)
    assert action is not None, "expected a TAP action"
    assert 1900 <= action.x <= 2300, f"expected x in [1900,2300], got {action}"
    assert action.y >= 900, f"expected bottom-row button (y>=900), got {action}"
