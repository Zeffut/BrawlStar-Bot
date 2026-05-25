"""Session recorder — capture phone screen at a fixed rate while the bot
plays, save frames + detected states to a session directory.

The recording can be inspected later (and replayed through
test_replay.py to regression-test strategy decisions).

Usage:
    python tools/session_recorder.py --minutes 5 --fps 2

Output:
    debug/sessions/<timestamp>/
        frames/
            00001.png
            00002.png
            ...
        events.jsonl     # state per frame + frame metadata

`session_recorder.py` does NOT touch the bot — it runs alongside it as an
observer. So you can run both at the same time:

    # Terminal 1: bot
    python -m bsbot.main

    # Terminal 2: recorder
    python tools/session_recorder.py --minutes 10
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "tools"))
from phone_controller import PhoneController  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
log = logging.getLogger("recorder")


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--minutes", type=float, default=2.0)
    p.add_argument("--fps", type=float, default=1.0, help="frames per second (rec rate)")
    p.add_argument("--label", default="session", help="folder name prefix")
    args = p.parse_args()

    pc = PhoneController()
    interval = 1.0 / args.fps
    out_dir = REPO_ROOT / "debug" / "sessions" / f"{args.label}-{time.strftime('%Y%m%d-%H%M%S')}"
    frames_dir = out_dir / "frames"
    frames_dir.mkdir(parents=True, exist_ok=True)
    events_file = out_dir / "events.jsonl"

    log.info("Recording to %s (%.1f min @ %.1f fps = %d frames target)",
             out_dir, args.minutes, args.fps, int(args.minutes * 60 * args.fps))

    import cv2
    n = 0
    end_at = time.monotonic() + args.minutes * 60
    with events_file.open("w", encoding="utf-8") as ev:
        while time.monotonic() < end_at:
            t0 = time.monotonic()
            try:
                frame = pc.screenshot()
            except Exception as exc:
                log.warning("screenshot failed: %s", exc)
                time.sleep(interval)
                continue
            state, score = pc.detect_state(frame)
            path = frames_dir / f"{n:05d}.png"
            cv2.imwrite(str(path), frame)
            ev.write(json.dumps({
                "n": n,
                "ts": time.time(),
                "frame": path.name,
                "shape": list(frame.shape),
                "state": state,
                "score": round(score, 3) if score else 0.0,
            }) + "\n")
            ev.flush()
            n += 1
            elapsed = time.monotonic() - t0
            if elapsed < interval:
                time.sleep(interval - elapsed)
            if n % 30 == 0:
                log.info("recorded %d frames", n)

    log.info("Done: %d frames in %s", n, out_dir)
    return 0


if __name__ == "__main__":
    sys.exit(main())
