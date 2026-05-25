"""Detect the high-level game state from a captured frame.

We use **template matching** (cv2.matchTemplate) instead of a dedicated ONNX
model: it's cheap, easy to extend (just drop a screenshot crop in
`data/state_templates/<state>/`), and good enough to distinguish lobby /
match / end / popup / disconnect screens by recognising their UI chrome
(buttons, banners, etc.).

Templates are organised as:

    data/state_templates/
        lobby/
            play_button.png
            shop_icon.png
        match/
            joystick_ring.png
            attack_button.png
        end/
            continue_button.png
        popup/
            close_x.png
        disconnect/
            reconnect_text.png

The state with the highest-scoring matching template wins, provided the
score crosses `threshold`. If nothing matches, returns "unknown".

NB: starts empty — templates are captured at runtime against the actual
device using `tools/capture_template.py`. Until then `detect()` returns
"unknown" and the bot falls back to safe defaults.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np

logger = logging.getLogger(__name__)


# Order matters: first match wins on ties. Disconnect screens look like menus,
# but require immediate action, so we check them first.
KNOWN_STATES: tuple[str, ...] = (
    "disconnect",
    "popup",
    "end",
    "starting",
    "lobby",
    "match",
)


@dataclass
class StateMatch:
    state: str
    template_name: str
    score: float
    location: tuple[int, int]  # top-left of match in frame


class StateFinder:
    """Load templates from a directory and detect the most likely state."""

    def __init__(self, templates_dir: str | Path, threshold: float = 0.85):
        self.templates_dir = Path(templates_dir)
        self.threshold = threshold
        # {state: [(name, template_gray_np), ...]}
        self._templates: dict[str, list[tuple[str, np.ndarray]]] = {}
        self.reload()

    def reload(self) -> None:
        """Re-scan the templates directory. Cheap; safe to call between sessions."""
        self._templates = {}
        if not self.templates_dir.exists():
            logger.warning("Templates dir does not exist: %s", self.templates_dir)
            return
        for state_dir in self.templates_dir.iterdir():
            if not state_dir.is_dir():
                continue
            state = state_dir.name
            entries: list[tuple[str, np.ndarray]] = []
            for img_path in sorted(state_dir.glob("*.png")):
                img = cv2.imread(str(img_path), cv2.IMREAD_GRAYSCALE)
                if img is None:
                    logger.warning("Could not read template %s", img_path)
                    continue
                entries.append((img_path.name, img))
            if entries:
                self._templates[state] = entries
        logger.info(
            "StateFinder loaded %d states (%s)",
            len(self._templates),
            ", ".join(f"{k}={len(v)}" for k, v in self._templates.items()) or "empty",
        )

    @property
    def n_templates(self) -> int:
        return sum(len(v) for v in self._templates.values())

    def detect(self, frame: np.ndarray) -> StateMatch | None:
        """Return the best `StateMatch` above threshold, or None if no match.

        `frame` may be BGR (from scrcpy) or grayscale; converted internally.
        """
        if self.n_templates == 0:
            return None

        if frame.ndim == 3:
            gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        else:
            gray = frame

        best: StateMatch | None = None
        for state in KNOWN_STATES:
            for name, template in self._templates.get(state, []):
                th, tw = template.shape[:2]
                fh, fw = gray.shape[:2]
                if th > fh or tw > fw:
                    continue  # template bigger than frame, skip
                result = cv2.matchTemplate(gray, template, cv2.TM_CCOEFF_NORMED)
                _min_val, max_val, _min_loc, max_loc = cv2.minMaxLoc(result)
                if max_val >= self.threshold and (best is None or max_val > best.score):
                    best = StateMatch(state=state, template_name=name, score=float(max_val), location=max_loc)
        return best

    def detect_state_name(self, frame: np.ndarray) -> str:
        """Convenience: just the state string ("lobby", "match", ...) or "unknown"."""
        match = self.detect(frame)
        return match.state if match else "unknown"
