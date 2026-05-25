"""SessionStats — track high-level bot KPIs during a session.

Updated by BrainWorker on state transitions (lobby→match = match started,
match→end = match ended). Periodically printed to console + appended to the
JSONL log.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field, asdict


@dataclass
class SessionStats:
    started_at: float = field(default_factory=time.time)
    frames_seen: int = 0
    matches_started: int = 0
    matches_completed: int = 0
    victories: int = 0   # set when we see "end" with winning indicator (TODO: needs UI parse)
    defeats: int = 0     # same
    disconnects: int = 0
    errors: int = 0
    # Performance.
    ips_samples: list[float] = field(default_factory=list)

    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False, compare=False)
    _last_state: str | None = field(default=None, repr=False, compare=False)
    _last_frame_at: float = field(default=0.0, repr=False, compare=False)

    # -------- mutation API -----------------------------------------------

    def record_frame(self) -> None:
        with self._lock:
            now = time.monotonic()
            if self._last_frame_at > 0:
                dt = now - self._last_frame_at
                if dt > 0:
                    self.ips_samples.append(1.0 / dt)
                    # Keep last N samples to bound memory.
                    if len(self.ips_samples) > 1000:
                        self.ips_samples = self.ips_samples[-1000:]
            self._last_frame_at = now
            self.frames_seen += 1

    def record_state_transition(self, new_state: str) -> None:
        with self._lock:
            prev = self._last_state
            self._last_state = new_state
            if prev == new_state:
                return
            if new_state == "match" and prev in ("lobby", None, "starting", "unknown"):
                self.matches_started += 1
            # Count a match as completed whenever we ENTER the end state,
            # regardless of the immediate predecessor (state often oscillates
            # match → unknown → end on real devices).
            if new_state == "end":
                self.matches_completed += 1
            if new_state == "disconnect":
                self.disconnects += 1

    def record_error(self) -> None:
        with self._lock:
            self.errors += 1

    # -------- read API ---------------------------------------------------

    def uptime_s(self) -> float:
        return time.time() - self.started_at

    def mean_ips(self) -> float | None:
        with self._lock:
            if not self.ips_samples:
                return None
            return sum(self.ips_samples) / len(self.ips_samples)

    def win_rate(self) -> float | None:
        with self._lock:
            total = self.victories + self.defeats
            if total == 0:
                return None
            return self.victories / total

    def snapshot(self) -> dict:
        """JSON-serializable view, safe to log/print."""
        with self._lock:
            ips = (
                sum(self.ips_samples) / len(self.ips_samples)
                if self.ips_samples else None
            )
            return {
                "uptime_s": round(self.uptime_s(), 1),
                "frames_seen": self.frames_seen,
                "mean_ips": round(ips, 2) if ips is not None else None,
                "matches_started": self.matches_started,
                "matches_completed": self.matches_completed,
                "victories": self.victories,
                "defeats": self.defeats,
                "win_rate": (
                    round(self.victories / (self.victories + self.defeats), 3)
                    if (self.victories + self.defeats) else None
                ),
                "disconnects": self.disconnects,
                "errors": self.errors,
            }
