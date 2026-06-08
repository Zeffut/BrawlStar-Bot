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
    def __init__(self, cfg: "dict | None" = None):
        # cfg passed explicitly (tests) → no file I/O / no hot-reload.
        self._from_files = cfg is None
        if cfg is None:
            cfg, self._mtimes = _load_layered_cfg()
        else:
            cfg = self._normalize_injected(cfg)
            self._mtimes = {}
        self._lock = threading.Lock()
        self._break_until = 0.0
        self._day: "str | None" = None
        self._matches_today = 0
        self._blocks_today = 0
        self._last_reload_check = 0.0
        self._active_pause_label = "pause"
        self._apply_cfg(cfg)
        # Today's resolved values (rolled in _ensure_day).
        self._today_cap = self.daily_cap
        self._today_block_cap = self.max_blocks
        self._today_sleep = self._base_sleep_window()
        self._today_pause_windows: "list[_Window]" = []
        self._today_is_dayoff = False
        if self.enabled and self.sleep_start == self.sleep_end and not self.pause_windows_cfg:
            log.warning("play schedule: sleep_start == sleep_end (%dh) and no "
                        "pause windows → 24h active.", self.sleep_start)

    # ---- compat properties for tests that read _today_sleep_start_min etc. ----
    @property
    def _today_sleep_start_min(self) -> int:
        return self._today_sleep.start_min

    @property
    def _today_sleep_end_min(self) -> int:
        return self._today_sleep.end_min

    # ---- config application ----
    @staticmethod
    def _normalize_injected(cfg: dict) -> dict:
        """Fill structural keys a test dict may omit so _apply_cfg is uniform."""
        out = dict(_DEFAULTS)
        out["pause_windows"] = []
        out["weekend"] = {}
        out["days"] = {}
        out.update(cfg)
        return out

    def _apply_cfg(self, cfg: dict) -> None:
        """(Re)apply tunables WITHOUT touching the current day's counters."""
        self.enabled = _as_bool(cfg["enabled"])
        self.timezone = str(cfg.get("timezone") or "Europe/Paris")
        self.sleep_start = int(cfg["sleep_start_hour"]) % 24
        self.sleep_end = int(cfg["sleep_end_hour"]) % 24
        self.sleep_jitter = max(0, int(cfg["sleep_jitter_minutes"]))
        self.block_min = int(cfg["block_min_minutes"])
        self.block_max = max(self.block_min, int(cfg["block_max_minutes"]))
        self.break_min = int(cfg["break_min_minutes"])
        self.break_max = max(self.break_min, int(cfg["break_max_minutes"]))
        self.daily_cap = int(cfg["daily_match_cap"])
        self.cap_jitter = int(cfg["daily_cap_jitter"])
        self.max_blocks = int(cfg.get("max_blocks_per_day", 0))
        self.blocks_jitter = int(cfg.get("blocks_jitter", 0))
        self.dayoff_weekdays = [str(d).strip().lower()
                                for d in (cfg.get("dayoff_weekdays") or [])]
        try:
            self.dayoff_chance = max(0.0, min(1.0, float(cfg.get("dayoff_chance", 0.0))))
        except (TypeError, ValueError):
            self.dayoff_chance = 0.0
        self.pause_windows_cfg = list(cfg.get("pause_windows") or [])
        self._overrides = {"weekend": cfg.get("weekend") or {},
                           "days": cfg.get("days") or {}}
        # Keep the raw base for per-day override resolution.
        self._base_cfg = dict(cfg)
        # Force a re-resolution of today's params on the next state() call.
        self._day = None

    def _base_sleep_window(self) -> "_Window":
        return _Window(self.sleep_start * 60, self.sleep_end * 60,
                       self.sleep_jitter, "sleep")

    @staticmethod
    def _parse_windows(raw_list) -> "list[_Window]":
        out: "list[_Window]" = []
        for item in raw_list or []:
            if not isinstance(item, dict):
                continue
            s = _parse_hhmm(item.get("start"))
            e = _parse_hhmm(item.get("end"))
            if s is None or e is None:
                continue
            out.append(_Window(s, e, int(item.get("jitter_minutes", 0) or 0),
                               str(item.get("label") or "pause")))
        return out

    # ---- hot-reload ----
    def _maybe_reload(self, now: float) -> None:
        """If a config file's mtime changed, re-apply tunables in place. Throttled
        to once / 10 s. Preserves the current day's counters + rolled values."""
        if not self._from_files:
            return
        if now - self._last_reload_check < 10:
            return
        self._last_reload_check = now
        cur = _mtimes(_config_paths())
        if cur == self._mtimes:
            return
        try:
            cfg, mt = _load_layered_cfg()
            self._apply_cfg(cfg)        # sets self._day = None
            self._mtimes = mt
            self._day = None            # force re-resolve today's params
            log.info("play schedule: config reloaded (mtime changed)")
        except Exception:
            log.debug("schedule hot-reload failed — keeping current config",
                      exc_info=True)

    # ---- daily bookkeeping ----
    def _ensure_day(self, now: float) -> None:
        day = time.strftime("%Y-%m-%d", time.localtime(now))
        # Forward-only counter reset: a backward clock jump (NTP correction,
        # manual change) crossing midnight must NOT zero the match counter —
        # that would let the bot exceed the daily cap. We DO still re-roll
        # the day's params (dayoff, sleep window, etc.) to answer correctly
        # for the queried timestamp, but counters are only reset going forward.
        if self._day is not None and day == self._day:
            return
        date_advanced = self._day is None or day > self._day
        if date_advanced:
            self._matches_today = 0
            self._blocks_today = 0
        self._day = day
        rng = random.Random("sched:" + day)
        lt = time.localtime(now)
        wd = lt.tm_wday
        params = _resolve_day_params(self._base_cfg, self._overrides, wd)
        cap = int(params.get("daily_match_cap", self.daily_cap))
        cj = int(params.get("daily_cap_jitter", self.cap_jitter))
        lo = max(1, cap - cj); hi = max(lo, cap + cj)
        self._today_cap = rng.randint(lo, hi)
        bcap = int(params.get("max_blocks_per_day", self.max_blocks))
        bj = int(params.get("blocks_jitter", self.blocks_jitter))
        if bcap > 0:
            blo = max(1, bcap - bj); bhi = max(blo, bcap + bj)
            self._today_block_cap = rng.randint(blo, bhi)
        else:
            self._today_block_cap = 0
        ss = int(params.get("sleep_start_hour", self.sleep_start)) % 24
        se = int(params.get("sleep_end_hour", self.sleep_end)) % 24
        sj = max(0, int(params.get("sleep_jitter_minutes", self.sleep_jitter)))
        self._today_sleep = _Window(ss * 60, se * 60, sj, "sleep").rolled(rng)
        raw_pw = params.get("pause_windows", self.pause_windows_cfg)
        self._today_pause_windows = [w.rolled(rng)
                                     for w in self._parse_windows(raw_pw)]
        off_days = [str(d).strip().lower()
                    for d in params.get("dayoff_weekdays", self.dayoff_weekdays)]
        chance = float(params.get("dayoff_chance", self.dayoff_chance) or 0.0)
        is_off = _WEEKDAY_NAMES[wd % 7] in off_days
        if not is_off and chance > 0.0:
            is_off = random.Random("dayoff:" + day).random() < chance
        self._today_is_dayoff = is_off
        if date_advanced:
            log.info("play schedule: new day %s (%s) — cap %d, blocks %s, "
                     "sleep %s–%s, %d pause windows%s", day,
                     _WEEKDAY_NAMES[wd % 7], self._today_cap,
                     self._today_block_cap or "∞",
                     _hhmm(self._today_sleep.start_min),
                     _hhmm(self._today_sleep.end_min),
                     len(self._today_pause_windows),
                     " — DAY OFF" if is_off else "")

    def record_match(self, now: "float | None" = None) -> None:
        now = now or time.time()
        with self._lock:
            self._ensure_day(now)
            self._matches_today += 1

    # ---- randomized durations ----
    def block_minutes(self) -> int:
        with self._lock:
            self._ensure_day(time.time())
            self._blocks_today += 1
        return random.randint(self.block_min, self.block_max)

    def start_break(self, now: "float | None" = None) -> int:
        mins = random.randint(self.break_min, self.break_max)
        with self._lock:
            self._break_until = time.monotonic() + mins * 60
        log.info("play schedule: break for %d min", mins)
        return mins

    # ---- the gate ----
    def state(self, now: "float | None" = None) -> str:
        if not self.enabled:
            return "play"
        now = now or time.time()
        with self._lock:
            self._maybe_reload(now)
            self._ensure_day(now)
            if self._today_is_dayoff:
                return "dayoff"
            lt = time.localtime(now)
            mod = lt.tm_hour * 60 + lt.tm_min
            if self._today_sleep.contains(mod):
                return "sleep"
            for w in self._today_pause_windows:
                if w.contains(mod):
                    self._active_pause_label = w.label
                    return "pause"
            if self._matches_today >= self._today_cap:
                return "cap"
            if self._today_block_cap and self._blocks_today >= self._today_block_cap:
                return "cap"
            if time.monotonic() < self._break_until:
                return "break"
            return "play"

    def should_play_now(self, now: "float | None" = None) -> "tuple[bool, str]":
        st = self.state(now)
        if st == "play":
            return True, "actif" if self.enabled else "schedule off"
        if st == "sleep":
            return False, (f"sommeil ({_hhmm(self._today_sleep.start_min)}–"
                           f"{_hhmm(self._today_sleep.end_min)})")
        if st == "pause":
            return False, f"pause ({self._active_pause_label})"
        if st == "cap":
            return False, f"quota du jour atteint ({self._matches_today}/{self._today_cap})"
        if st == "dayoff":
            return False, "jour de repos"
        return False, "pause"


_SCHEDULE: "PlaySchedule | None" = None
_GET_LOCK = threading.Lock()


def get() -> PlaySchedule:
    global _SCHEDULE
    if _SCHEDULE is None:
        with _GET_LOCK:
            if _SCHEDULE is None:
                _SCHEDULE = PlaySchedule()
                log.info("play schedule loaded: enabled=%s sleep≈%dh-%dh±%dmin "
                         "block=%d-%dmin break=%d-%dmin cap≈%d±%d blocks≈%s "
                         "pause_windows=%d dayoff_days=%s chance=%.2f",
                         _SCHEDULE.enabled, _SCHEDULE.sleep_start, _SCHEDULE.sleep_end,
                         _SCHEDULE.sleep_jitter, _SCHEDULE.block_min, _SCHEDULE.block_max,
                         _SCHEDULE.break_min, _SCHEDULE.break_max,
                         _SCHEDULE.daily_cap, _SCHEDULE.cap_jitter,
                         _SCHEDULE.max_blocks or "∞", len(_SCHEDULE.pause_windows_cfg),
                         _SCHEDULE.dayoff_weekdays, _SCHEDULE.dayoff_chance)
    return _SCHEDULE
