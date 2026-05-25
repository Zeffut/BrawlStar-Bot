"""VisionWorker — read the latest frame, infer state + detections, publish GameState."""
from __future__ import annotations

import logging
import threading
import time
from pathlib import Path

import numpy as np

from bsbot.buses import LatestSlot
from bsbot.vision.detect import Detect
from bsbot.vision.postprocess import GameState, build_match_state
from bsbot.vision.state_finder import StateFinder

logger = logging.getLogger(__name__)


# Class lists for each ONNX model. PylaAI keeps them in cfg/bot_config.toml &
# dynamic class lists fetched from API; for our self-hosted bot we hardcode
# the ones we care about. `mainInGameModel` and `brawlersInGame` class lists
# can grow over time; unknown class indices are just returned as numeric keys
# by Detect when `classes=None`, which is fine for v1.
TILE_CLASSES = ["wall", "bush", "close_bush"]

# brawlersInGame classes — full brawler roster, kept in JSON for maintenance.
# When None, Detect returns numeric class IDs.
BRAWLERS_CLASSES: list[str] | None = None


class VisionWorker(threading.Thread):
    def __init__(
        self,
        frame_slot: LatestSlot[np.ndarray],
        state_slot: LatestSlot[GameState],
        stop_event: threading.Event,
        models_dir: str | Path,
        templates_dir: str | Path,
        my_brawler_class: str = "colt",
        preferred_device: str = "auto",
        state_finder_threshold: float = 0.85,
        run_full_detection_on_states: tuple[str, ...] = ("match", "unknown"),
    ):
        super().__init__(name="VisionWorker", daemon=True)
        self.frame_slot = frame_slot
        self.state_slot = state_slot
        self.stop_event = stop_event
        self.models_dir = Path(models_dir)
        self.templates_dir = Path(templates_dir)
        self.my_brawler_class = my_brawler_class
        self.preferred_device = preferred_device
        self.run_full_detection_on_states = run_full_detection_on_states

        self.state_finder = StateFinder(self.templates_dir, threshold=state_finder_threshold)
        self._frame_id = 0
        self._tile_detector: Detect | None = None
        self._brawlers_detector: Detect | None = None
        self._main_detector: Detect | None = None

    def _load_models(self) -> None:
        """Lazy-load ONNX detectors. Heavy (1-3s per model on CoreML)."""
        if self._tile_detector is None:
            self._tile_detector = Detect(
                str(self.models_dir / "tileDetector.onnx"),
                classes=TILE_CLASSES,
                preferred_device=self.preferred_device,
            )
        # PylaAI uses mainInGameModel with explicit class names for entity
        # detection. brawlersInGame is for brawler-identity, not what we need.
        if self._main_detector is None:
            self._main_detector = Detect(
                str(self.models_dir / "mainInGameModel.onnx"),
                classes=["enemy", "teammate", "player"],
                preferred_device=self.preferred_device,
            )
        # brawlersInGame kept lazy but not used in match loop anymore.
        if self._brawlers_detector is None:
            self._brawlers_detector = Detect(
                str(self.models_dir / "brawlersInGame.onnx"),
                classes=BRAWLERS_CLASSES,
                preferred_device=self.preferred_device,
            )

    def run(self) -> None:
        logger.info("VisionWorker loading models…")
        try:
            self._load_models()
        except Exception as exc:
            logger.exception("Failed to load ONNX models: %s", exc)
            self.stop_event.set()
            return
        logger.info("VisionWorker ready.")

        last_seen_version = 0
        errors_in_a_row = 0
        max_consecutive_errors = 10

        while not self.stop_event.is_set():
            frame, version = self.frame_slot.wait_new(last_seen_version, timeout=0.5)
            if frame is None or version == last_seen_version:
                continue
            last_seen_version = version
            self._frame_id += 1
            try:
                gs = self._process_frame(frame)
                self.state_slot.set(gs)
                errors_in_a_row = 0
            except Exception as exc:
                errors_in_a_row += 1
                logger.exception(
                    "VisionWorker frame %d failed (%d/%d): %s",
                    self._frame_id,
                    errors_in_a_row,
                    max_consecutive_errors,
                    exc,
                )
                if errors_in_a_row >= max_consecutive_errors:
                    logger.error("Too many vision errors in a row, stopping bot.")
                    self.stop_event.set()
                    break

        logger.info("VisionWorker stopped.")

    def _ensure_pool(self):
        if not hasattr(self, "_pool"):
            from concurrent.futures import ThreadPoolExecutor
            self._pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="onnx")

    def _process_frame(self, frame: np.ndarray) -> GameState:
        h, w = frame.shape[:2]
        # State detection is expensive (~190ms with scale=0.5). Cache it and
        # re-run rarely:
        # - In match: every 10 frames (state stays match for entire game)
        # - Outside match: every 3 frames (transitions matter for menus)
        if not hasattr(self, "_state_cache"):
            self._state_cache = None
            self._state_cache_frame_id = -1
        prev_state = self._state_cache.state if self._state_cache else None
        cache_every = 10 if prev_state == "match" else 3
        if self._state_cache is None or (self._frame_id - self._state_cache_frame_id) >= cache_every:
            self._state_cache = self.state_finder.detect(frame)
            self._state_cache_frame_id = self._frame_id
            if self._state_cache and self._frame_id % 30 == 0:
                logger.info("state_finder: %s via %s (%.3f)",
                            self._state_cache.state, self._state_cache.template_name,
                            self._state_cache.score)
        state_match = self._state_cache
        state_name = state_match.state if state_match else "unknown"

        if state_name not in self.run_full_detection_on_states:
            # Cheap path: only publish the state, no expensive ONNX.
            return GameState(
                state=state_name,
                frame_id=self._frame_id,
                timestamp=time.time(),
                frame_width=w,
                frame_height=h,
            )

        # Run tile + main in parallel.
        # mainInGameModel returns {enemy, teammate, player} with explicit
        # class labels — this gives us self position + enemy targets
        # without confusing walls/bushes as brawlers.
        assert self._tile_detector and self._main_detector
        self._ensure_pool()
        f_tile = self._pool.submit(self._tile_detector.detect_objects, frame, 0.6)
        f_main = self._pool.submit(self._main_detector.detect_objects, frame, 0.6)
        tile_raw = f_tile.result()
        main_raw = f_main.result()
        # We pass `main_raw` AS the brawler_raw because mainInGameModel now
        # plays that role with proper class labels.
        brawler_raw = main_raw

        # If state_finder said "unknown" but we got brawlers, assume match.
        effective_state = "match" if (state_name == "unknown" and brawler_raw) else state_name

        # Conversely: if state_finder says "match" but the brawler model
        # finds zero plausible enemies, it's almost certainly a false
        # positive (loading screen, popup with match background, etc.).
        # Downgrade so MenuStrategy can handle the dismiss.
        if effective_state == "match" and not brawler_raw:
            effective_state = "unknown"

        if effective_state == "match":
            return build_match_state(
                frame_id=self._frame_id,
                frame_width=w,
                frame_height=h,
                tile_raw=tile_raw,
                brawler_raw=brawler_raw,
                main_raw=main_raw,
                my_brawler_class=self.my_brawler_class,
            )
        return GameState(
            state=effective_state,  # type: ignore[arg-type]
            frame_id=self._frame_id,
            timestamp=time.time(),
            frame_width=w,
            frame_height=h,
        )
