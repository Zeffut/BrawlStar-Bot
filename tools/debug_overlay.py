"""Debug overlay — show everything the bot sees on a single annotated image.

Captures one (or many) phone screenshots and overlays:
  * state_finder result (top banner)
  * brawlersInGame detections (red boxes + class id + assumed enemies)
  * tileDetector detections (yellow=wall, green=bush)
  * mainInGameModel detections (cyan)
  * assumed my_pos crosshair (white cross at frame center)
  * UI button positions calibrated in ButtonLayout + MenuCoords
  * computed shoot direction toward closest enemy

Usage:
    python tools/debug_overlay.py                # single shot, saves to debug/
    python tools/debug_overlay.py --loop 30      # one shot every 1.5s for 30 frames
    python tools/debug_overlay.py --send         # also push the image to chat
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from pathlib import Path

import cv2
import numpy as np

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))
sys.path.insert(0, str(REPO_ROOT / "tools"))

os.environ.setdefault("ORT_LOGGING_LEVEL", "3")

from bsbot.vision.detect import Detect  # noqa: E402
from bsbot.vision.state_finder import StateFinder  # noqa: E402
from bsbot.vision.postprocess import build_match_state, parse_brawler_detections  # noqa: E402
from bsbot.strategies.menu import MenuCoords  # noqa: E402
from bsbot.workers.control import ButtonLayout  # noqa: E402
from bsbot.utils.geometry import distance  # noqa: E402
from phone_controller import PhoneController  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
log = logging.getLogger("overlay")

MODELS_DIR = REPO_ROOT / "src" / "bsbot" / "models"
DEBUG_DIR = REPO_ROOT / "debug"
DEBUG_DIR.mkdir(exist_ok=True)


def load_detectors():
    log.info("Loading ONNX models…")
    return {
        "tile": Detect(str(MODELS_DIR / "tileDetector.onnx"), classes=["wall", "bush", "close_bush"]),
        "brawler": Detect(str(MODELS_DIR / "brawlersInGame.onnx")),
        "main": Detect(str(MODELS_DIR / "mainInGameModel.onnx")),
    }


def compute_detections(
    frame: np.ndarray,
    state_finder: StateFinder,
    detectors: dict,
    conf_thresh: float = 0.5,
) -> dict:
    """Run all models and return a dict of detection results to be drawn later.

    Decoupled from drawing so the slow ONNX work can run at a low rate
    while a fast UI thread redraws over fresh raw frames.
    """
    h, w = frame.shape[:2]
    sm = state_finder.detect(frame)
    tile_raw = detectors["tile"].detect_objects(frame, conf_thresh=conf_thresh)
    brawler_raw = detectors["brawler"].detect_objects(frame, conf_thresh=conf_thresh)
    main_raw = detectors["main"].detect_objects(frame, conf_thresh=conf_thresh)
    from bsbot.vision.postprocess import parse_brawler_detections
    my_pos, _ = parse_brawler_detections(brawler_raw, frame_width=w, frame_height=h)
    return {
        "width": w, "height": h,
        "state": sm.state if sm else "unknown",
        "state_score": round(sm.score, 3) if sm else 0.0,
        "tile_raw": tile_raw,
        "brawler_raw": brawler_raw,
        "main_raw": main_raw,
        "my_pos": my_pos,
    }


def draw_from_detections(frame: np.ndarray, det: dict) -> tuple[np.ndarray, dict]:
    """Draw overlay using PRE-COMPUTED detections on a fresh raw frame.

    Frame motion stays fluid while overlay reflects slightly-stale detections.
    """
    img = frame.copy()
    n_walls = n_bushes = n_brawlers = n_main = 0
    brawler_boxes: list[tuple[int, int]] = []
    for cls, boxes in det.get("tile_raw", {}).items():
        for xyxy in boxes:
            x1, y1, x2, y2 = map(int, xyxy)
            if cls == "wall":
                color = (0, 255, 255); n_walls += 1
            else:
                color = (0, 255, 0); n_bushes += 1
            cv2.rectangle(img, (x1, y1), (x2, y2), color, 2)
    for cls, boxes in det.get("brawler_raw", {}).items():
        for xyxy in boxes:
            x1, y1, x2, y2 = map(int, xyxy)
            cv2.rectangle(img, (x1, y1), (x2, y2), (0, 0, 255), 3)
            n_brawlers += 1
            brawler_boxes.append(((x1 + x2) // 2, (y1 + y2) // 2))
    for cls, boxes in det.get("main_raw", {}).items():
        for xyxy in boxes:
            x1, y1, x2, y2 = map(int, xyxy)
            cv2.rectangle(img, (x1, y1), (x2, y2), (255, 255, 0), 2)
            n_main += 1
    my_pos = det.get("my_pos")
    if my_pos:
        cv2.drawMarker(img, my_pos, (255, 255, 255), markerType=cv2.MARKER_CROSS,
                       markerSize=60, thickness=3)
        if brawler_boxes:
            from bsbot.utils.geometry import distance as _dist
            target = min(brawler_boxes, key=lambda p: (p[0] - my_pos[0]) ** 2 + (p[1] - my_pos[1]) ** 2)
            d = _dist(my_pos, target)
            cv2.arrowedLine(img, my_pos, target, (255, 0, 255), 3, tipLength=0.05)
    # Banner with summary on top.
    h, w = img.shape[:2]
    banner = np.zeros((70, w, 3), dtype=np.uint8)
    txt = (f"STATE: {det.get('state','?')}  score={det.get('state_score',0):.2f}  | "
           f"brawlers={n_brawlers}  walls={n_walls}  bushes={n_bushes}  main={n_main}")
    cv2.putText(banner, txt, (20, 45), cv2.FONT_HERSHEY_SIMPLEX, 0.85, (255, 255, 255), 2, cv2.LINE_AA)
    img = np.vstack([banner, img])
    info = {
        "state": det.get("state"), "brawlers": n_brawlers,
        "walls": n_walls, "bushes": n_bushes, "main": n_main,
    }
    return img, info


def annotate(
    frame: np.ndarray,
    state_finder: StateFinder,
    detectors: dict,
    conf_thresh: float = 0.5,
) -> tuple[np.ndarray, dict]:
    """Draw all overlays. Returns annotated frame + summary dict."""
    img = frame.copy()
    h, w = img.shape[:2]
    info: dict = {"width": w, "height": h}

    # 1. State.
    sm = state_finder.detect(frame)
    state = sm.state if sm else "unknown"
    state_score = sm.score if sm else 0.0
    info["state"] = state
    info["state_score"] = round(state_score, 3) if state_score else 0.0

    # 2. Detections (only run if in match for performance, else only tile+main).
    tile_raw = detectors["tile"].detect_objects(frame, conf_thresh=conf_thresh)
    brawler_raw = detectors["brawler"].detect_objects(frame, conf_thresh=conf_thresh)
    main_raw = detectors["main"].detect_objects(frame, conf_thresh=conf_thresh)

    # Tile boxes (walls yellow, bushes green).
    n_walls = n_bushes = 0
    for cls, boxes in tile_raw.items():
        for xyxy in boxes:
            x1, y1, x2, y2 = map(int, xyxy)
            if cls == "wall":
                color = (0, 255, 255)  # yellow BGR
                n_walls += 1
            else:
                color = (0, 255, 0)  # green
                n_bushes += 1
            cv2.rectangle(img, (x1, y1), (x2, y2), color, 2)
            cv2.putText(img, cls, (x1, max(0, y1 - 4)),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.5, color, 1, cv2.LINE_AA)
    info["walls"] = n_walls
    info["bushes"] = n_bushes

    # Brawler boxes (red).
    n_brawlers = 0
    brawler_boxes: list[tuple[int, int]] = []
    for cls, boxes in brawler_raw.items():
        for xyxy in boxes:
            x1, y1, x2, y2 = map(int, xyxy)
            cx, cy = (x1 + x2) // 2, (y1 + y2) // 2
            n_brawlers += 1
            brawler_boxes.append((cx, cy))
            cv2.rectangle(img, (x1, y1), (x2, y2), (0, 0, 255), 3)
            cv2.putText(img, f"brawler[{cls}]", (x1, max(0, y1 - 4)),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 0, 255), 2, cv2.LINE_AA)
    info["brawlers"] = n_brawlers

    # Main model boxes (cyan).
    n_main = 0
    for cls, boxes in main_raw.items():
        for xyxy in boxes:
            x1, y1, x2, y2 = map(int, xyxy)
            cv2.rectangle(img, (x1, y1), (x2, y2), (255, 255, 0), 2)
            cv2.putText(img, f"main[{cls}]", (x1, max(0, y1 - 4)),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 255, 0), 1, cv2.LINE_AA)
            n_main += 1
    info["main"] = n_main

    # 3. Assumed my_pos (center, 55% down — same heuristic as the bot).
    my_pos, _ = parse_brawler_detections(brawler_raw, frame_width=w, frame_height=h)
    if my_pos is not None:
        mx, my = my_pos
        cv2.drawMarker(img, (mx, my), (255, 255, 255), markerType=cv2.MARKER_CROSS,
                       markerSize=60, thickness=3)
        cv2.putText(img, "MY_POS", (mx + 30, my - 10),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.7, (255, 255, 255), 2, cv2.LINE_AA)
        info["my_pos"] = my_pos

        # Closest brawler distance + arrow.
        if brawler_boxes:
            target = min(brawler_boxes, key=lambda p: (p[0] - mx) ** 2 + (p[1] - my) ** 2)
            d = distance(my_pos, target)
            cv2.arrowedLine(img, my_pos, target, (255, 0, 255), 3, tipLength=0.05)
            cv2.putText(img, f"target d={d:.0f}px", (target[0] + 10, target[1]),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.7, (255, 0, 255), 2, cv2.LINE_AA)
            info["closest_brawler_dist_px"] = round(d, 1)

    # 4. UI buttons (from calibration).
    bl = ButtonLayout()
    mc = MenuCoords()
    button_color = (200, 50, 255)  # purple
    for name, pos, color in [
        ("joystick", bl.joystick_center, (100, 255, 100)),
        ("attack", bl.attack_button, (50, 50, 255)),
        ("super", bl.super_button, (50, 150, 255)),
        ("gadget", bl.gadget_button, (200, 100, 255)),
        ("play", mc.play_button, (200, 200, 50)),
        ("continue", mc.continue_button, (200, 200, 50)),
        ("home", mc.popup_close, (50, 200, 200)),
        ("reconnect", mc.reconnect_button, (50, 200, 200)),
    ]:
        x, y = pos
        cv2.circle(img, (x, y), 25, color, 3)
        cv2.putText(img, name, (x - 30, y + 50),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.6, color, 2, cv2.LINE_AA)

    # 5. Top banner — state + counts.
    banner_h = 90
    banner = np.zeros((banner_h, w, 3), dtype=np.uint8)
    txt = (
        f"STATE: {state}  score={info['state_score']:.2f}  | "
        f"brawlers={n_brawlers}  walls={n_walls}  bushes={n_bushes}  main={n_main}"
    )
    cv2.putText(banner, txt, (20, 35),
                cv2.FONT_HERSHEY_SIMPLEX, 0.85, (255, 255, 255), 2, cv2.LINE_AA)
    if my_pos is not None and info.get("closest_brawler_dist_px"):
        sub = f"my_pos={my_pos}  closest_brawler={info['closest_brawler_dist_px']}px"
        cv2.putText(banner, sub, (20, 70),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.65, (200, 200, 200), 1, cv2.LINE_AA)
    img = np.vstack([banner, img])

    # 6. Legend at the bottom of the banner area on the right.
    legend_h = 30
    legend = np.zeros((legend_h, w, 3), dtype=np.uint8)
    items = [
        ("brawler", (0, 0, 255)),
        ("wall", (0, 255, 255)),
        ("bush", (0, 255, 0)),
        ("main", (255, 255, 0)),
        ("my_pos", (255, 255, 255)),
        ("buttons", (200, 50, 255)),
    ]
    x = 20
    for name, color in items:
        cv2.rectangle(legend, (x, 8), (x + 20, 22), color, -1)
        cv2.putText(legend, name, (x + 25, 22),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.55, (255, 255, 255), 1, cv2.LINE_AA)
        x += 130
    img = np.vstack([img, legend])

    return img, info


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--loop", type=int, default=0, help="number of frames (default 0 = single shot)")
    p.add_argument("--interval-s", type=float, default=1.5)
    p.add_argument("--conf", type=float, default=0.5)
    p.add_argument("--out", type=str, default=None)
    args = p.parse_args()

    pc = PhoneController()
    sf = pc.state_finder
    detectors = load_detectors()

    if args.loop > 0:
        for i in range(args.loop):
            frame = pc.screenshot()
            img, info = annotate(frame, sf, detectors, conf_thresh=args.conf)
            ts = time.strftime("%Y%m%d-%H%M%S")
            out = DEBUG_DIR / f"overlay_{ts}_{i:03d}.png"
            cv2.imwrite(str(out), img)
            log.info("[%d/%d] %s — %s", i + 1, args.loop, out.name, info)
            time.sleep(args.interval_s)
    else:
        frame = pc.screenshot()
        img, info = annotate(frame, sf, detectors, conf_thresh=args.conf)
        out = Path(args.out) if args.out else (DEBUG_DIR / f"overlay_{time.strftime('%Y%m%d-%H%M%S')}.png")
        cv2.imwrite(str(out), img)
        log.info("Saved %s", out)
        log.info("Info: %s", info)
        # Print path on its own line for easy capture by callers.
        print(str(out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
