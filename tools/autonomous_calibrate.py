"""Autonomous calibration: watch the phone, capture transitions, save debug
screenshots of all states we don't yet recognise.

Usage:
    python tools/autonomous_calibrate.py [--max-minutes N]

The script runs forever (or until --max-minutes). Whenever the detected state
changes (or stays "unknown" for too long), it saves a timestamped screenshot
into ./debug/ so a human can inspect what the bot saw.

This is how we bootstrap templates for new states (end, popup, disconnect, …)
without watching the phone manually.
"""
from __future__ import annotations

import argparse
import logging
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from phone_controller import PhoneController

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
log = logging.getLogger("autocalib")


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--max-minutes", type=float, default=10.0)
    p.add_argument("--poll-s", type=float, default=1.5)
    args = p.parse_args()

    pc = PhoneController()
    log.info("Autonomous calibration started — will run for %.1f min", args.max_minutes)

    last_state: str | None = None
    last_unknown_save = 0.0
    deadline = time.monotonic() + args.max_minutes * 60
    transitions = 0

    while time.monotonic() < deadline:
        frame = pc.screenshot()
        state, score = pc.detect_state(frame)

        if state != last_state:
            transitions += 1
            log.info("Transition #%d: %s → %s (score=%s)", transitions, last_state, state, score)
            ts = time.strftime("%Y%m%d-%H%M%S")
            path = Path("debug") / f"{ts}_transition_{transitions:03d}_{state}.png"
            import cv2
            cv2.imwrite(str(path), frame)
            log.info("  Saved %s", path)
            last_state = state
            # Reset unknown timer on transition.
            last_unknown_save = time.monotonic()
        elif state == "unknown" and (time.monotonic() - last_unknown_save) > 8:
            # If we're stuck in unknown for >8s, save a snapshot — likely a new state we don't recognise.
            ts = time.strftime("%Y%m%d-%H%M%S")
            path = Path("debug") / f"{ts}_unknown_persist.png"
            import cv2
            cv2.imwrite(str(path), frame)
            log.info("Saved persistent-unknown screenshot to %s", path)
            last_unknown_save = time.monotonic()

        time.sleep(args.poll_s)

    log.info("Calibration loop finished after %d transitions", transitions)
    return 0


if __name__ == "__main__":
    sys.exit(main())
