"""Humane play schedule — stops the bot from grinding 24/7.

The bot used to run ~10h blocks back-to-back (re-resumed by the keepalive) and
played ~500 matches/day non-stop — a pattern no human produces. This module
shapes the activity into something believable AND aligned with the economics
(the valuable resource, gold, is calendar-capped at ~330/day, so a short daily
session already captures it — running 24/7 only produced low-value trophies):

  • a nightly SLEEP WINDOW (no play),
  • inside the active window, randomized PLAY BLOCKS alternating with BREAKS,
  • a randomized DAILY MATCH CAP.

Everything is randomized within ranges so the cadence isn't periodic. The worker
consults `should_play_now()` before (re)starting a session and uses
`block_minutes()` to bound each run; `record_match()` feeds the daily cap.

Config lives in cfg/general_config.toml under a [schedule] table (sensible
defaults if absent). Set enabled=false there to restore 24/7 behavior.
"""
from __future__ import annotations

import logging
import random
import threading
import time

log = logging.getLogger("play_schedule")

_DEFAULTS = {
    "enabled": True,
    "sleep_start_hour": 1,      # local time: no play from 01:00 …
    "sleep_end_hour": 9,        # … until 09:00 (≈8h "sleep")
    "block_min_minutes": 40,    # a play block lasts 40–85 min
    "block_max_minutes": 85,
    "break_min_minutes": 20,    # then a break of 20–70 min
    "break_max_minutes": 70,
    "daily_match_cap": 180,     # ~130–230 matches/day (vs ~520 at 24/7)
    "daily_cap_jitter": 50,
}


def _load_cfg() -> dict:
    cfg = dict(_DEFAULTS)
    try:
        from utils import load_toml_as_dict
        section = (load_toml_as_dict("cfg/general_config.toml") or {}).get("schedule", {})
        for k, v in (section or {}).items():
            if k in cfg and v is not None:
                cfg[k] = type(cfg[k])(v) if not isinstance(cfg[k], bool) else _as_bool(v)
    except Exception:
        log.debug("schedule config load failed — using defaults", exc_info=True)
    return cfg


def _as_bool(v) -> bool:
    if isinstance(v, bool):
        return v
    return str(v).strip().lower() in ("1", "true", "yes", "on")


class PlaySchedule:
    def __init__(self, cfg: dict | None = None):
        cfg = cfg or _load_cfg()
        self.enabled = _as_bool(cfg["enabled"])
        self.sleep_start = int(cfg["sleep_start_hour"]) % 24
        self.sleep_end = int(cfg["sleep_end_hour"]) % 24
        self.block_min = int(cfg["block_min_minutes"])
        self.block_max = max(self.block_min, int(cfg["block_max_minutes"]))
        self.break_min = int(cfg["break_min_minutes"])
        self.break_max = max(self.break_min, int(cfg["break_max_minutes"]))
        self.daily_cap = int(cfg["daily_match_cap"])
        self.cap_jitter = int(cfg["daily_cap_jitter"])
        self._lock = threading.Lock()
        self._break_until = 0.0
        self._day: str | None = None
        self._matches_today = 0
        self._today_cap = self.daily_cap
        if self.enabled and self.sleep_start == self.sleep_end:
            log.warning("play schedule: sleep_start == sleep_end (%dh) → NO sleep "
                        "window (24h active). Set different hours to enable sleep.",
                        self.sleep_start)

    # ---- daily bookkeeping ----
    def _ensure_day(self, now: float) -> None:
        day = time.strftime("%Y-%m-%d", time.localtime(now))
        # Reset only when the date ADVANCES. A backward clock jump (NTP
        # correction, manual change) crossing midnight would otherwise re-roll
        # the cap and zero the counter → the daily cap could be exceeded twice,
        # defeating the whole point of the cap. Forward-only is conservative.
        if self._day is None or day > self._day:
            self._day = day
            self._matches_today = 0
            lo = max(1, self.daily_cap - self.cap_jitter)
            hi = max(lo, self.daily_cap + self.cap_jitter)
            self._today_cap = random.randint(lo, hi)
            log.info("play schedule: new day %s — match cap %d", day, self._today_cap)

    def record_match(self, now: float | None = None) -> None:
        now = now or time.time()
        with self._lock:
            self._ensure_day(now)
            self._matches_today += 1

    # ---- randomized durations ----
    def block_minutes(self) -> int:
        return random.randint(self.block_min, self.block_max)

    def start_break(self, now: float | None = None) -> int:
        """Begin a break of a randomized length. Returns its minutes.

        Tracked on the MONOTONIC clock (immune to NTP/DST jumps that would
        otherwise stretch or cancel a break)."""
        mins = random.randint(self.break_min, self.break_max)
        with self._lock:
            self._break_until = time.monotonic() + mins * 60
        log.info("play schedule: break for %d min", mins)
        return mins

    # ---- the gate ----
    def _is_sleep_hour(self, h: int) -> bool:
        if self.sleep_start == self.sleep_end:
            return False
        if self.sleep_start < self.sleep_end:        # e.g. 1 → 9
            return self.sleep_start <= h < self.sleep_end
        return h >= self.sleep_start or h < self.sleep_end  # wraps midnight

    def should_play_now(self, now: float | None = None) -> tuple[bool, str]:
        """(can_play, reason). Reason is human-readable for status/logging."""
        if not self.enabled:
            return True, "schedule off"
        now = now or time.time()
        with self._lock:
            self._ensure_day(now)
            h = time.localtime(now).tm_hour
            if self._is_sleep_hour(h):
                return False, f"sommeil ({self.sleep_start}h–{self.sleep_end}h)"
            if self._matches_today >= self._today_cap:
                return False, f"quota du jour atteint ({self._matches_today}/{self._today_cap})"
            # Break is on the monotonic clock (set by start_break), independent
            # of the wall-clock `now` used for the sleep window / daily reset.
            mono = time.monotonic()
            if mono < self._break_until:
                left = int((self._break_until - mono) / 60) + 1
                return False, f"pause (~{left} min)"
            return True, "actif"


_SCHEDULE: PlaySchedule | None = None
_GET_LOCK = threading.Lock()


def get() -> PlaySchedule:
    global _SCHEDULE
    if _SCHEDULE is None:
        with _GET_LOCK:
            if _SCHEDULE is None:
                _SCHEDULE = PlaySchedule()
                log.info("play schedule loaded: enabled=%s sleep=%dh-%dh "
                         "block=%d-%dmin break=%d-%dmin cap≈%d±%d",
                         _SCHEDULE.enabled, _SCHEDULE.sleep_start, _SCHEDULE.sleep_end,
                         _SCHEDULE.block_min, _SCHEDULE.block_max,
                         _SCHEDULE.break_min, _SCHEDULE.break_max,
                         _SCHEDULE.daily_cap, _SCHEDULE.cap_jitter)
    return _SCHEDULE
