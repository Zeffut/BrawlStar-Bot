"""Collect a labeled fixture for the test suite.

Captures the current phone screen and saves it to
`tests/fixtures/states/<state>/<name>_<timestamp>.png`.

Usage:
    python tools/fixture_collect.py --state match --name brawl_ball_score1_0
    python tools/fixture_collect.py --state popup --name pass_brawl_offer

Differences vs `capture_template.py`:
- This saves a FULL screenshot for **regression testing** (not a small crop).
- The captured image is used by `test_state_finder_regression.py` to ensure
  state detection keeps working after template changes.
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "tools"))
from phone_controller import PhoneController  # noqa: E402

KNOWN_STATES = ("lobby", "match", "end", "popup", "starting", "disconnect")


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--state", required=True, choices=KNOWN_STATES)
    p.add_argument("--name", required=True, help="short slug e.g. 'brawl_ball_winning'")
    args = p.parse_args()

    pc = PhoneController()
    frame = pc.screenshot()
    out_dir = REPO_ROOT / "tests" / "fixtures" / "states" / args.state
    out_dir.mkdir(parents=True, exist_ok=True)
    ts = time.strftime("%Y%m%d-%H%M%S")
    out = out_dir / f"{args.name}_{ts}.png"
    import cv2
    cv2.imwrite(str(out), frame)
    print(f"Saved fixture: {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
