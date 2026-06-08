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

_WEEKDAY_NAMES = ["monday", "tuesday", "wednesday", "thursday", "friday",
                  "saturday", "sunday"]

_DEFAULTS = {
    "enabled": True,
    "timezone": "Europe/Paris",   # informational; OS tz governs the clock
    "sleep_start_hour": 1,
    "sleep_end_hour": 9,
    "sleep_jitter_minutes": 40,
    "block_min_minutes": 40,
    "block_max_minutes": 85,
    "break_min_minutes": 20,
    "break_max_minutes": 70,
    "daily_match_cap": 180,
    "daily_cap_jitter": 50,
    "max_blocks_per_day": 0,      # 0 = unlimited
    "blocks_jitter": 0,
    "dayoff_weekdays": [],        # e.g. ["sunday"]
    "dayoff_chance": 0.0,         # 0..1 probability per day (seeded)
    # pause_windows is a list of dicts, handled separately (not a scalar).
}

# Keys whose value type is a plain scalar coerced to int.
_SCALAR_KEYS = ("sleep_start_hour", "sleep_end_hour", "sleep_jitter_minutes",
                "block_min_minutes", "block_max_minutes", "break_min_minutes",
                "break_max_minutes", "daily_match_cap", "daily_cap_jitter",
                "max_blocks_per_day", "blocks_jitter")


def _resolve_day_params(base: dict, overrides: dict, weekday: int) -> dict:
    """Resolve effective params for a given weekday (0=Mon..6=Sun).

    base ← weekend (if Sat/Sun and present) ← days.<weekday-name> (if present).
    Later layers win, key by key. Returns a new dict (base untouched).
    """
    out = dict(base)
    if weekday >= 5:  # Saturday / Sunday
        for k, v in (overrides.get("weekend") or {}).items():
            out[k] = v
    day_name = _WEEKDAY_NAMES[weekday % 7]
    for k, v in ((overrides.get("days") or {}).get(day_name) or {}).items():
        out[k] = v
    return out


def _as_bool(v) -> bool:
    if isinstance(v, bool):
        return v
    return str(v).strip().lower() in ("1", "true", "yes", "on")


def _config_paths() -> "list[str]":
    return ["cfg/general_config.toml", "cfg/schedule.local.toml"]


def _mtimes(paths) -> dict:
    import os
    out = {}
    for p in paths:
        try:
            out[p] = os.path.getmtime(p)
        except OSError:
            out[p] = None
    return out


def _coerce_into(cfg: dict, section: dict) -> None:
    """Merge a [schedule] section into cfg in place, coercing scalar types and
    parsing the structured keys (dayoff_weekdays, pause_windows, overrides)."""
    for k, v in (section or {}).items():
        if v is None:
            continue
        if k == "enabled":
            cfg["enabled"] = _as_bool(v)
        elif k == "timezone":
            cfg["timezone"] = str(v)
        elif k == "dayoff_chance":
            try:
                cfg["dayoff_chance"] = max(0.0, min(1.0, float(v)))
            except (TypeError, ValueError):
                pass
        elif k == "dayoff_weekdays":
            if isinstance(v, (list, tuple)):
                cfg["dayoff_weekdays"] = [str(x).strip().lower() for x in v]
        elif k == "pause_windows":
            if isinstance(v, (list, tuple)):
                cfg["pause_windows"] = list(v)
        elif k in ("weekend", "days"):
            cfg[k] = v
        elif k in _SCALAR_KEYS:
            try:
                cfg[k] = int(v)
            except (TypeError, ValueError):
                pass


def _load_layered_cfg() -> "tuple[dict, dict]":
    """Load the 3-layer config. Returns (resolved_cfg, mtimes).

    Layers (later wins): _DEFAULTS ← general_config.toml[schedule] ←
    schedule.local.toml[schedule] (or its flat top-level keys).
    """
    cfg = dict(_DEFAULTS)
    cfg["pause_windows"] = []
    cfg["weekend"] = {}
    cfg["days"] = {}
    paths = _config_paths()
    try:
        from utils import load_toml_as_dict
        base = load_toml_as_dict(paths[0]) or {}
        _coerce_into(cfg, base.get("schedule", {}) or {})
        import os
        if os.path.exists(paths[1]):
            local = load_toml_as_dict(paths[1]) or {}
            section = local.get("schedule", local)
            _coerce_into(cfg, section or {})
    except Exception:
        log.debug("schedule config load failed — using defaults", exc_info=True)
    return cfg, _mtimes(paths)


def _load_cfg() -> dict:
    # Backward-compat shim: existing callers expect a flat cfg dict.
    return _load_layered_cfg()[0]


def _hhmm(minute_of_day: int) -> str:
    m = int(minute_of_day) % 1440
    return "%02dh%02d" % (m // 60, m % 60)


def _parse_hhmm(s) -> "int | None":
    """Parse 'HH:MM' (or a bare 'HH') into minutes-of-day [0..1439]. None if junk."""
    if s is None:
        return None
    txt = str(s).strip()
    if not txt:
        return None
    try:
        if ":" in txt:
            h, m = txt.split(":", 1)
            mins = int(h) * 60 + int(m)
        else:
            mins = int(txt) * 60
    except (ValueError, TypeError):
        return None
    if mins < 0 or mins > 1439:
        return None
    return mins


class _Window:
    """A daily closed time window [start, end) in minutes-of-day, with optional
    per-day jitter. Handles midnight wrap (start > end). Reused for the nightly
    sleep window AND the daytime pause windows."""

    __slots__ = ("start_min", "end_min", "jitter", "label")

    def __init__(self, start_min: int, end_min: int, jitter: int = 0,
                 label: str = "pause"):
        self.start_min = int(start_min) % 1440
        self.end_min = int(end_min) % 1440
        self.jitter = max(0, int(jitter))
        self.label = label or "pause"

    def contains(self, minute_of_day: int) -> bool:
        s, e = self.start_min, self.end_min
        if s == e:
            return False
        if s < e:
            return s <= minute_of_day < e
        return minute_of_day >= s or minute_of_day < e   # wraps midnight

    def rolled(self, rng) -> "_Window":
        """Return a copy with start/end jittered by ±jitter (same rng → same
        result). Empty windows (start==end) are returned unchanged."""
        if not self.jitter or self.start_min == self.end_min:
            return _Window(self.start_min, self.end_min, 0, self.label)
        s = (self.start_min + rng.randint(-self.jitter, self.jitter)) % 1440
        e = (self.end_min + rng.randint(-self.jitter, self.jitter)) % 1440
        return _Window(s, e, 0, self.label)


class PlaySchedule:
    def __init__(self, cfg: dict | None = None):
        cfg = cfg or _load_cfg()
        self.enabled = _as_bool(cfg["enabled"])
        self.sleep_start = int(cfg["sleep_start_hour"]) % 24
        self.sleep_end = int(cfg["sleep_end_hour"]) % 24
        # Bed/wake times wobble ±sleep_jitter each day so the schedule isn't a
        # robotic on-the-hour boundary. Base window in minutes-of-day; the
        # per-day jittered values are rolled in _ensure_day.
        self.sleep_jitter = max(0, int(cfg.get("sleep_jitter_minutes",
                                               _DEFAULTS["sleep_jitter_minutes"])))
        self._sleep_start_min0 = self.sleep_start * 60
        self._sleep_end_min0 = self.sleep_end * 60
        self._today_sleep_start_min = self._sleep_start_min0
        self._today_sleep_end_min = self._sleep_end_min0
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
            # Seed a per-day RNG on the date string so the same day always
            # yields the SAME cap + bed/wake times even across worker restarts
            # (a restart must not re-roll the cap — that could let the bot
            # exceed it — nor shift the wake time it's already sleeping toward).
            rng = random.Random("sched:" + day)
            lo = max(1, self.daily_cap - self.cap_jitter)
            hi = max(lo, self.daily_cap + self.cap_jitter)
            self._today_cap = rng.randint(lo, hi)
            if self.sleep_jitter and self.sleep_start != self.sleep_end:
                j = self.sleep_jitter
                self._today_sleep_start_min = (self._sleep_start_min0
                                               + rng.randint(-j, j)) % 1440
                self._today_sleep_end_min = (self._sleep_end_min0
                                             + rng.randint(-j, j)) % 1440
            else:
                self._today_sleep_start_min = self._sleep_start_min0
                self._today_sleep_end_min = self._sleep_end_min0
            log.info("play schedule: new day %s — match cap %d, sleep %s–%s",
                     day, self._today_cap,
                     _hhmm(self._today_sleep_start_min),
                     _hhmm(self._today_sleep_end_min))

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
    def _is_sleep_minute(self, minute_of_day: int) -> bool:
        """Whether `minute_of_day` (0–1439, local) falls in TODAY's jittered
        sleep window. Caller must hold the lock (reads _today_sleep_*)."""
        start, end = self._today_sleep_start_min, self._today_sleep_end_min
        if start == end:
            return False
        if start < end:                              # e.g. 01:00 → 09:00
            return start <= minute_of_day < end
        return minute_of_day >= start or minute_of_day < end  # wraps midnight

    def state(self, now: float | None = None) -> str:
        """Current schedule state: 'play' | 'break' | 'sleep' | 'cap'.

        'sleep'/'cap' are LONG pauses (close the game). 'break' is a short
        in-session pause (leave the game open for a quick resume)."""
        if not self.enabled:
            return "play"
        now = now or time.time()
        with self._lock:
            self._ensure_day(now)
            lt = time.localtime(now)
            if self._is_sleep_minute(lt.tm_hour * 60 + lt.tm_min):
                return "sleep"
            if self._matches_today >= self._today_cap:
                return "cap"
            # Break is on the MONOTONIC clock (set by start_break), independent
            # of the wall-clock `now` used for the sleep window / daily reset.
            if time.monotonic() < self._break_until:
                return "break"
            return "play"

    def should_play_now(self, now: float | None = None) -> tuple[bool, str]:
        """(can_play, reason). Reason is human-readable for status/logging."""
        st = self.state(now)
        if st == "play":
            return True, "actif" if self.enabled else "schedule off"
        if st == "sleep":
            return False, (f"sommeil ({_hhmm(self._today_sleep_start_min)}–"
                           f"{_hhmm(self._today_sleep_end_min)})")
        if st == "cap":
            return False, f"quota du jour atteint ({self._matches_today}/{self._today_cap})"
        return False, "pause"


_SCHEDULE: PlaySchedule | None = None
_GET_LOCK = threading.Lock()


def get() -> PlaySchedule:
    global _SCHEDULE
    if _SCHEDULE is None:
        with _GET_LOCK:
            if _SCHEDULE is None:
                _SCHEDULE = PlaySchedule()
                log.info("play schedule loaded: enabled=%s sleep≈%dh-%dh±%dmin "
                         "block=%d-%dmin break=%d-%dmin cap≈%d±%d",
                         _SCHEDULE.enabled, _SCHEDULE.sleep_start, _SCHEDULE.sleep_end,
                         _SCHEDULE.sleep_jitter,
                         _SCHEDULE.block_min, _SCHEDULE.block_max,
                         _SCHEDULE.break_min, _SCHEDULE.break_max,
                         _SCHEDULE.daily_cap, _SCHEDULE.cap_jitter)
    return _SCHEDULE
