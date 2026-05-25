"""BrawlStar-Bot v2 — entry point.

Boots all four workers (Capture → Vision → Brain → Control), wires them via
buses, and runs until SIGINT.

Usage:
    python -m bsbot.main           # default config (./config.toml)
    python -m bsbot.main --config path/to/config.toml
    python -m bsbot.main --dry-run # skip ADB / scrcpy; useful for smoke checks
"""
from __future__ import annotations

import argparse
import logging
import signal
import sys
import threading
import time
import tomllib
from pathlib import Path

from bsbot.buses import ControlBus, LatestSlot
from bsbot.controls.adb import AdbController
from bsbot.strategies.colt import BrawlerStats, ColtStrategy, ColtTuning
from bsbot.strategies.menu import MenuCoords, MenuStrategy
from bsbot.utils.logging import default_session_dir, setup_logging
from bsbot.vision.postprocess import GameState
from bsbot.workers.brain import BrainWorker
from bsbot.workers.capture import CaptureWorker
from bsbot.workers.control import ButtonLayout, ControlWorker
from bsbot.workers.vision import VisionWorker

logger = logging.getLogger("bsbot.main")

REPO_ROOT = Path(__file__).resolve().parent
MODELS_DIR = REPO_ROOT / "models"
DATA_DIR = REPO_ROOT / "data"
TEMPLATES_DIR = DATA_DIR / "state_templates"
DEFAULT_CONFIG = Path(__file__).resolve().parent.parent.parent / "config.toml"


def load_config(path: Path) -> dict:
    with path.open("rb") as f:
        return tomllib.load(f)


def build_strategies(config: dict) -> dict:
    """Wire MenuStrategy + ColtStrategy based on config."""
    brawler = config.get("game", {}).get("brawler", "colt")
    stats = BrawlerStats.load(DATA_DIR / "brawlers_info.json", brawler)
    colt_strategy = ColtStrategy(stats=stats, tuning=ColtTuning())
    menu_strategy = MenuStrategy(MenuCoords())
    return {
        "match": colt_strategy,
        "lobby": menu_strategy,
        "end": menu_strategy,
        "popup": menu_strategy,
        "disconnect": menu_strategy,
        "starting": menu_strategy,
    }


def run(config: dict, dry_run: bool = False) -> int:
    """Boot the bot. Returns process exit code."""
    log_path = setup_logging(
        level=config.get("debug", {}).get("log_level", "INFO"),
        session_dir=default_session_dir(),
    )
    if log_path:
        logger.info("Session log: %s", log_path)

    stop_event = threading.Event()

    def _sigint(_signum, _frame):
        logger.info("SIGINT received, stopping…")
        stop_event.set()

    signal.signal(signal.SIGINT, _sigint)
    signal.signal(signal.SIGTERM, _sigint)

    # Buses.
    frame_slot: LatestSlot = LatestSlot()
    state_slot: LatestSlot[GameState] = LatestSlot()
    control_bus = ControlBus()

    # ADB.
    if dry_run:
        logger.warning("Dry run: skipping ADB connection.")
        adb = None
    else:
        serial = config.get("adb", {}).get("device_serial", "") or ""
        try:
            adb = AdbController.connect(serial=serial)
            logger.info(
                "Connected to ADB device %s (%dx%d)",
                adb.device.serial, adb.screen_width, adb.screen_height,
            )
        except Exception as exc:
            logger.error("ADB connection failed: %s", exc)
            return 2

    # Strategies.
    try:
        strategies = build_strategies(config)
    except Exception as exc:
        logger.exception("Failed to build strategies: %s", exc)
        return 3

    # Workers.
    workers: list[threading.Thread] = []
    if not dry_run:
        capture = CaptureWorker(
            frame_slot,
            stop_event,
            device_serial=config.get("adb", {}).get("device_serial", "") or "",
            max_width=config.get("performance", {}).get("scrcpy_max_size", 1280),
            bitrate=config.get("performance", {}).get("scrcpy_bitrate", 8_000_000),
        )
        workers.append(capture)

    vision = VisionWorker(
        frame_slot,
        state_slot,
        stop_event,
        models_dir=MODELS_DIR,
        templates_dir=TEMPLATES_DIR,
        my_brawler_class=config.get("game", {}).get("brawler", "colt"),
        preferred_device=config.get("performance", {}).get("inference_device", "auto"),
    )
    workers.append(vision)

    brain = BrainWorker(state_slot, control_bus, stop_event, strategies=strategies)
    workers.append(brain)

    if not dry_run and adb is not None:
        control = ControlWorker(control_bus, adb, stop_event, layout=ButtonLayout())
        workers.append(control)

    # Start everything.
    for w in workers:
        w.start()
    logger.info("Bot running. Ctrl-C to stop.")

    # Session limits.
    session_cfg = config.get("session", {})
    max_minutes = int(session_cfg.get("max_duration_minutes", 0))
    start = time.monotonic()

    try:
        while not stop_event.is_set():
            time.sleep(1.0)
            if max_minutes > 0 and (time.monotonic() - start) >= max_minutes * 60:
                logger.info("Session duration limit reached, stopping.")
                stop_event.set()
                break
    finally:
        stop_event.set()
        for w in workers:
            w.join(timeout=3.0)
        logger.info("Bot stopped.")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--config", type=Path, default=DEFAULT_CONFIG, help="Path to config.toml")
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Don't connect to phone; useful for verifying imports + ONNX load.",
    )
    args = p.parse_args()
    if not args.config.exists():
        print(f"Config file not found: {args.config}", file=sys.stderr)
        return 1
    config = load_config(args.config)
    return run(config, dry_run=args.dry_run)


if __name__ == "__main__":
    sys.exit(main())
