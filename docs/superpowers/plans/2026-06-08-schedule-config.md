# Planning de jeu ultra-configurable + hot-reload — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Rendre le planning pause/jeu entièrement paramétrable (fenêtres de pause diurnes, overrides week-end/par-jour, jour de repos, plafond de blocs) et modifiable à chaud, sans casser l'existant.

**Architecture:** Refonte de `play_schedule.py` autour de petites unités pures (helper de fenêtre, loader en couches, résolution d'overrides) + `PlaySchedule` qui porte les tunables et l'état du jour, avec hot-reload par mtime. Config en 3 couches (defaults → `general_config.toml[schedule]` → `cfg/schedule.local.toml` gitignorée). Un seul point d'intégration worker change (`_manage_schedule_powersave` : `!= "play"` ferme le jeu).

**Tech Stack:** Python 3.9 (local) / 3.12 (worker), TOML via `utils.load_toml_as_dict`, pytest. Tests purs sans deps lourdes (`PlaySchedule(cfg_dict)` bypasse l'I/O fichier).

**Référence spec:** `docs/superpowers/specs/2026-06-08-schedule-config-design.md`

**Conventions clés (à respecter) :**
- `PlaySchedule(cfg: dict)` doit continuer de fonctionner SANS I/O fichier (les tests injectent un dict). Le hot-reload ne s'active que quand l'objet est construit via `get()` (chargement fichier, mtimes connus).
- État du jour seedé sur la date (`random.Random("sched:"+day)`) → stable au sein d'un jour et au restart. Un reload NE re-tire PAS le quota.
- Tout `!= "play"` = pause + fermeture du jeu côté worker.
- Heures = heure locale du worker (tz OS, garder Europe/Paris).

---

## File Structure

- `play_schedule.py` — réécrit : helpers purs (`_parse_hhmm`, `_Window`, `_resolve_day_params`, `_load_layered_cfg`) + `PlaySchedule` + singleton `get()`.
- `tests/test_play_schedule.py` — étendu (cas existants conservés + nouveaux).
- `cfg/general_config.toml` — section `[schedule]` ré-documentée + exemples commentés.
- `cfg/schedule.local.toml.example` — gabarit de la couche locale.
- `.gitignore` — ignore `cfg/schedule.local.toml`.
- `telegram_main.py` — `_manage_schedule_powersave` généralisé (`!= "play"` + label map).

---

## Task 1 : Helper de fenêtre horaire (`_Window` + `_parse_hhmm`)

**Files:**
- Modify: `play_schedule.py` (ajouter en haut, après `_hhmm`)
- Test: `tests/test_play_schedule.py`

- [ ] **Step 1 : Écrire les tests qui échouent**

Ajouter en bas de `tests/test_play_schedule.py` :

```python
from play_schedule import _parse_hhmm, _Window


def test_parse_hhmm():
    assert _parse_hhmm("01:00") == 60
    assert _parse_hhmm("12:30") == 750
    assert _parse_hhmm("9") == 540          # bare hour
    assert _parse_hhmm("23:59") == 1439
    assert _parse_hhmm("bad") is None
    assert _parse_hhmm("") is None


def test_window_contains_simple():
    w = _Window(start_min=60, end_min=540, jitter=0, label="sleep")  # 01:00–09:00
    assert w.contains(180)        # 03:00 inside
    assert not w.contains(600)    # 10:00 outside
    assert w.contains(60)         # inclusive start
    assert not w.contains(540)    # exclusive end


def test_window_contains_wraps_midnight():
    w = _Window(start_min=1380, end_min=420, jitter=0, label="sleep")  # 23:00–07:00
    assert w.contains(1410)       # 23:30 inside
    assert w.contains(60)         # 01:00 inside (after midnight)
    assert not w.contains(600)    # 10:00 outside


def test_window_empty_when_start_eq_end():
    w = _Window(start_min=300, end_min=300, jitter=0, label="x")
    assert not w.contains(300)
    assert not w.contains(0)


def test_window_roll_jittered_deterministic_and_bounded():
    import random
    w = _Window(start_min=60, end_min=540, jitter=40, label="sleep")
    r1 = w.rolled(random.Random("sched:2026-06-08"))
    r2 = w.rolled(random.Random("sched:2026-06-08"))
    assert (r1.start_min, r1.end_min) == (r2.start_min, r2.end_min)   # same seed
    assert abs(((r1.start_min - 60 + 720) % 1440) - 720) <= 40        # within ±40
```

- [ ] **Step 2 : Lancer — vérifier l'échec**

Run: `python3 -m pytest tests/test_play_schedule.py -k "window or parse_hhmm" -q`
Expected: FAIL (`ImportError: cannot import name '_parse_hhmm'`).

- [ ] **Step 3 : Implémenter `_parse_hhmm` + `_Window`**

Dans `play_schedule.py`, juste après la fonction `_hhmm` :

```python
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
```

- [ ] **Step 4 : Lancer — vérifier le succès**

Run: `python3 -m pytest tests/test_play_schedule.py -k "window or parse_hhmm" -q`
Expected: PASS.

- [ ] **Step 5 : Commit**

```bash
git add play_schedule.py tests/test_play_schedule.py
git commit -m "feat(schedule): reusable _Window + _parse_hhmm helpers"
```

---

## Task 2 : Loader en couches + résolution d'overrides (`_load_layered_cfg`, `_resolve_day_params`)

**Files:**
- Modify: `play_schedule.py`
- Test: `tests/test_play_schedule.py`

- [ ] **Step 1 : Écrire les tests qui échouent**

Ajouter à `tests/test_play_schedule.py` :

```python
from play_schedule import _resolve_day_params, _DEFAULTS


def test_resolve_day_params_base_only():
    base = {"daily_match_cap": 180, "sleep_start_hour": 1}
    out = _resolve_day_params(base, {}, weekday=2)   # mercredi
    assert out["daily_match_cap"] == 180
    assert out["sleep_start_hour"] == 1


def test_resolve_day_params_weekend_override():
    base = {"daily_match_cap": 180, "sleep_start_hour": 1}
    overrides = {"weekend": {"daily_match_cap": 260, "sleep_start_hour": 2}}
    sat = _resolve_day_params(base, overrides, weekday=5)   # samedi
    assert sat["daily_match_cap"] == 260 and sat["sleep_start_hour"] == 2
    wed = _resolve_day_params(base, overrides, weekday=2)   # mercredi
    assert wed["daily_match_cap"] == 180                    # base inchangée


def test_resolve_day_params_per_day_beats_weekend():
    base = {"daily_match_cap": 180}
    overrides = {"weekend": {"daily_match_cap": 260},
                 "days": {"sunday": {"daily_match_cap": 90}}}
    sun = _resolve_day_params(base, overrides, weekday=6)   # dimanche
    assert sun["daily_match_cap"] == 90                     # days.sunday gagne
    sat = _resolve_day_params(base, overrides, weekday=5)   # samedi
    assert sat["daily_match_cap"] == 260                    # weekend
```

- [ ] **Step 2 : Lancer — vérifier l'échec**

Run: `python3 -m pytest tests/test_play_schedule.py -k resolve_day_params -q`
Expected: FAIL (`ImportError`).

- [ ] **Step 3 : Implémenter `_DEFAULTS` étendu, `_resolve_day_params`, `_load_layered_cfg`**

Remplacer le `_DEFAULTS` existant dans `play_schedule.py` par :

```python
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

# Keys whose value type is a plain scalar coerced via type(default)(v).
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
```

Puis remplacer la fonction `_load_cfg` par `_load_layered_cfg` :

```python
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
        # Base committed config.
        base = load_toml_as_dict(paths[0]) or {}
        _coerce_into(cfg, base.get("schedule", {}) or {})
        # Local override (gitignored). Accept either [schedule] or flat keys.
        import os
        if os.path.exists(paths[1]):
            local = load_toml_as_dict(paths[1]) or {}
            section = local.get("schedule", local)
            _coerce_into(cfg, section or {})
    except Exception:
        log.debug("schedule config load failed — using defaults", exc_info=True)
    return cfg, _mtimes(paths)
```

- [ ] **Step 4 : Lancer — vérifier le succès**

Run: `python3 -m pytest tests/test_play_schedule.py -k resolve_day_params -q`
Expected: PASS.

- [ ] **Step 5 : Commit**

```bash
git add play_schedule.py tests/test_play_schedule.py
git commit -m "feat(schedule): layered config loader + weekend/per-day override resolution"
```

---

## Task 3 : `PlaySchedule` — fenêtres, pause diurne, jour de repos, plafond de blocs

**Files:**
- Modify: `play_schedule.py` (réécrire la classe `PlaySchedule` + `get()`)
- Test: `tests/test_play_schedule.py`

- [ ] **Step 1 : Écrire les tests qui échouent**

Ajouter à `tests/test_play_schedule.py` :

```python
def _ts_on(weekday: int, hour: int) -> float:
    """Timestamp at given LOCAL hour on the NEXT date matching `weekday` (0=Mon)."""
    import datetime as _dt
    d = _dt.date.today()
    while d.weekday() != weekday:
        d += _dt.timedelta(days=1)
    lt = time.struct_time((d.year, d.month, d.day, hour, 30, 0,
                           weekday, 0, -1))
    return time.mktime(lt)


def test_pause_window_blocks_play():
    cfg = {**CFG, "pause_windows": [{"start": "12:00", "end": "13:00",
                                     "jitter_minutes": 0, "label": "déjeuner"}]}
    s = PlaySchedule(cfg)
    ok, why = s.should_play_now(_ts(12))     # 12:30 — inside the lunch window
    assert not ok and "déjeuner" in why
    assert s.state(_ts(12)) == "pause"
    assert s.should_play_now(_ts(15))[0]     # 15:30 — active


def test_max_blocks_per_day_caps():
    s = PlaySchedule({**CFG, "max_blocks_per_day": 2, "blocks_jitter": 0})
    now = _ts(14)
    s.block_minutes(); s.block_minutes()     # consume 2 blocks
    ok, why = s.should_play_now(now)
    assert not ok and "quota" in why
    assert s.state(now) == "cap"


def test_dayoff_explicit_weekday():
    s = PlaySchedule({**CFG, "dayoff_weekdays": ["sunday"]})
    sun = _ts_on(6, 15)                       # Sunday 15:30
    ok, why = s.should_play_now(sun)
    assert not ok and "repos" in why
    assert s.state(sun) == "dayoff"
    assert s.should_play_now(_ts_on(2, 15))[0]   # Wednesday active


def test_dayoff_chance_deterministic():
    # chance=1.0 → every day is off; chance=0.0 → never.
    assert PlaySchedule({**CFG, "dayoff_chance": 1.0}).state(_ts(15)) == "dayoff"
    assert PlaySchedule({**CFG, "dayoff_chance": 0.0}).state(_ts(15)) != "dayoff"


def test_weekend_override_changes_cap():
    cfg = {**CFG, "daily_match_cap": 5, "daily_cap_jitter": 0,
           "weekend": {"daily_match_cap": 2}}
    s = PlaySchedule(cfg)
    sat = _ts_on(5, 14)
    s.record_match(sat); s.record_match(sat)
    assert not s.should_play_now(sat)[0]      # weekend cap=2 hit
```

- [ ] **Step 2 : Lancer — vérifier l'échec**

Run: `python3 -m pytest tests/test_play_schedule.py -k "pause_window or max_blocks or dayoff or weekend_override" -q`
Expected: FAIL.

- [ ] **Step 3 : Réécrire la classe `PlaySchedule`**

Remplacer toute la classe `PlaySchedule` ET la fonction `get()` dans `play_schedule.py` par :

```python
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
            saved_day = self._day
            self._apply_cfg(cfg)        # sets self._day = None
            self._mtimes = mt
            self._day = None            # force re-resolve today's params
            log.info("play schedule: config reloaded (mtime changed)")
            _ = saved_day               # counters preserved (not reset here)
        except Exception:
            log.debug("schedule hot-reload failed — keeping current config",
                      exc_info=True)

    # ---- daily bookkeeping ----
    def _ensure_day(self, now: float) -> None:
        day = time.strftime("%Y-%m-%d", time.localtime(now))
        if self._day is not None and day <= self._day:
            return
        # New day (or first call / forced re-resolve): roll today's params.
        # NB: counters reset only when the DATE advances, never on a re-resolve
        # triggered by a config reload mid-day.
        date_advanced = self._day is None or day > self._day
        if date_advanced:
            self._matches_today = 0
            self._blocks_today = 0
        self._day = day
        rng = random.Random("sched:" + day)
        lt = time.localtime(now)
        wd = lt.tm_wday
        params = _resolve_day_params(self._base_cfg, self._overrides, wd)
        # Effective scalars for the day.
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
        # Sleep window (jittered) for the day.
        ss = int(params.get("sleep_start_hour", self.sleep_start)) % 24
        se = int(params.get("sleep_end_hour", self.sleep_end)) % 24
        sj = max(0, int(params.get("sleep_jitter_minutes", self.sleep_jitter)))
        self._today_sleep = _Window(ss * 60, se * 60, sj, "sleep").rolled(rng)
        # Daytime pause windows (jittered).
        raw_pw = params.get("pause_windows", self.pause_windows_cfg)
        self._today_pause_windows = [w.rolled(rng)
                                     for w in self._parse_windows(raw_pw)]
        # Day off?
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
            return False, f"pause ({getattr(self, '_active_pause_label', 'pause')})"
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
```

NOTE : supprimer l'ancienne méthode `_is_sleep_minute` (remplacée par `_Window.contains`) et l'ancien `_ensure_day`. Garder `_hhmm`, `_as_bool`.

- [ ] **Step 4 : Lancer TOUTE la suite play_schedule (régression incluse)**

Run: `python3 -m pytest tests/test_play_schedule.py -q`
Expected: PASS (anciens + nouveaux). Si un ancien test casse, corriger la compat — les anciennes clés doivent produire le même comportement.

- [ ] **Step 5 : Commit**

```bash
git add play_schedule.py tests/test_play_schedule.py
git commit -m "feat(schedule): pause windows, day-off, block cap, weekend/per-day overrides"
```

---

## Task 4 : Hot-reload (test du rechargement par mtime)

**Files:**
- Test: `tests/test_play_schedule.py`
- Modify: `play_schedule.py` (uniquement si un bug est trouvé — le code reload est déjà en Task 3)

- [ ] **Step 1 : Écrire le test qui échoue**

```python
def test_hot_reload_picks_up_new_mtime(tmp_path, monkeypatch):
    """Hot-reload without the heavy `utils`/`toml` dep: inject a fake utils
    module whose load_toml_as_dict reads our tmp TOML via a tiny parser, and
    drive the config purely through file mtimes."""
    import sys, types, play_schedule as ps

    # Minimal TOML-ish loader sufficient for flat [schedule] scalar keys.
    def _tiny_load(path):
        out = {"schedule": {}}
        try:
            with open(path) as fh:
                for line in fh:
                    line = line.strip()
                    if not line or line.startswith("#") or line.startswith("["):
                        continue
                    k, _, v = line.partition("=")
                    k = k.strip(); v = v.strip()
                    if v in ("true", "false"):
                        out["schedule"][k] = (v == "true")
                    else:
                        try: out["schedule"][k] = int(v)
                        except ValueError: out["schedule"][k] = v.strip('"')
        except OSError:
            pass
        return out

    fake_utils = types.ModuleType("utils")
    fake_utils.load_toml_as_dict = _tiny_load
    monkeypatch.setitem(sys.modules, "utils", fake_utils)

    base = tmp_path / "general_config.toml"
    local = tmp_path / "schedule.local.toml"
    base.write_text(
        "[schedule]\nenabled = true\nsleep_start_hour = 1\nsleep_end_hour = 9\n"
        "block_min_minutes = 40\nblock_max_minutes = 85\n"
        "break_min_minutes = 20\nbreak_max_minutes = 70\n"
        "daily_match_cap = 200\ndaily_cap_jitter = 0\n")
    monkeypatch.setattr(ps, "_config_paths", lambda: [str(base), str(local)])

    s = ps.PlaySchedule()                 # loads from files via fake utils
    now = _ts(14)
    assert s.daily_cap == 200
    # Edit the local override to a lower cap (new file → new mtime).
    local.write_text("[schedule]\ndaily_match_cap = 100\ndaily_cap_jitter = 0\n")
    s._last_reload_check = 0.0             # bypass the 10 s throttle
    s.state(now + 1)                       # triggers _maybe_reload
    assert s.daily_cap == 100             # tunables updated in place
```

- [ ] **Step 2 : Lancer — vérifier**

Run: `python3 -m pytest tests/test_play_schedule.py -k hot_reload -q`
Expected: PASS (le code de reload est en place depuis Task 3). Si FAIL, corriger `_maybe_reload`/`_apply_cfg` jusqu'au vert.

- [ ] **Step 3 : Commit**

```bash
git add tests/test_play_schedule.py play_schedule.py
git commit -m "test(schedule): hot-reload picks up config mtime changes"
```

---

## Task 5 : Intégration worker (`_manage_schedule_powersave`)

**Files:**
- Modify: `telegram_main.py:263-276` (le bloc de pause de `_manage_schedule_powersave`)

- [ ] **Step 1 : Généraliser la fermeture du jeu à tout état ≠ play**

Remplacer dans `telegram_main.py` le bloc actuel :

```python
    if st in ("sleep", "cap", "break") and not bot.runner._power_saved:
        try:
            api.enter_power_save()
            bot.runner._power_saved = True
            log.info("play schedule: %s → power-save (Brawl Stars closed, screen off)", st)
        except Exception:
            log.exception("schedule enter_power_save failed")
    label = {"sleep": "sommeil", "cap": "quota du jour", "break": "pause"}.get(st, st)
```

par :

```python
    # Any non-"play" state is a pause → close the game + screen off. Generalized
    # from an explicit allowlist so new schedule states (pause windows, day off)
    # close the game automatically.
    if st != "play" and not bot.runner._power_saved:
        try:
            api.enter_power_save()
            bot.runner._power_saved = True
            log.info("play schedule: %s → power-save (Brawl Stars closed, screen off)", st)
        except Exception:
            log.exception("schedule enter_power_save failed")
    label = {"sleep": "sommeil", "cap": "quota du jour", "break": "pause",
             "pause": "pause", "dayoff": "jour de repos"}.get(st, st)
```

- [ ] **Step 2 : Vérifier la compilation + pyflakes**

Run: `python3 -m py_compile telegram_main.py && python3 -m pyflakes telegram_main.py`
Expected: aucune sortie d'erreur.

- [ ] **Step 3 : Commit**

```bash
git add telegram_main.py
git commit -m "feat(schedule): any non-play state closes the game (pause windows, day off)"
```

---

## Task 6 : Fichiers de config documentés

**Files:**
- Modify: `cfg/general_config.toml` (section `[schedule]`)
- Create: `cfg/schedule.local.toml.example`
- Modify: `.gitignore`

- [ ] **Step 1 : Ré-documenter `[schedule]` dans `cfg/general_config.toml`**

Remplacer toute la section `[schedule]` existante par (garder les valeurs actuelles comme base) :

```toml
# ============================================================================
# Humane play schedule (play_schedule.py) — keeps the bot from grinding 24/7.
# Toutes les heures sont en HEURE LOCALE du worker (garder le HP sur Europe/Paris).
# Config en 3 couches : defaults code ← ce fichier ← cfg/schedule.local.toml
# (gitignorée, éditable en live sur le HP). Hot-reload : modifier puis sauver
# applique sans redémarrer le worker (sous ~10 s). enabled=false → 24/7.
# ============================================================================
[schedule]
enabled = true
timezone = "Europe/Paris"     # informatif (warn si ≠ tz système) ; l'OS gouverne

# --- Sommeil nocturne ---
sleep_start_hour = 1          # coucher ~01:00 …
sleep_end_hour = 9            # … réveil ~09:00 (~8h)
sleep_jitter_minutes = 40     # coucher/réveil varient ±40 min/jour (humain)

# --- Blocs de jeu / pauses courtes ---
block_min_minutes = 40        # un bloc dure 40–85 min …
block_max_minutes = 85
break_min_minutes = 20        # … puis une pause de 20–70 min (jeu fermé)
break_max_minutes = 70
max_blocks_per_day = 0        # 0 = illimité ; sinon plafonne le nb de blocs/jour
blocks_jitter = 0             # ±jitter sur ce plafond

# --- Quota de matchs/jour ---
daily_match_cap = 180         # ~130–230 matchs/jour
daily_cap_jitter = 50

# --- Jour de repos (journée entière sans jeu) ---
dayoff_weekdays = []          # jours fixes, ex. ["sunday"]
dayoff_chance = 0.0           # proba qu'un jour soit OFF (0..1, déterministe/jour)

# --- Fenêtres de pause diurnes (jeu fermé pendant) ---
# Décommenter / ajouter autant que voulu :
# [[schedule.pause_windows]]
# start = "12:30"
# end = "13:30"
# jitter_minutes = 20
# label = "déjeuner"
#
# [[schedule.pause_windows]]
# start = "18:00"
# end = "19:30"
# jitter_minutes = 30
# label = "soirée"

# --- Overrides week-end (samedi+dimanche) : surcharge n'importe quelle clé ---
# [schedule.weekend]
# sleep_start_hour = 2
# daily_match_cap = 260

# --- Overrides par jour précis : surcharge n'importe quelle clé ---
# [schedule.days.sunday]
# daily_match_cap = 90
```

- [ ] **Step 2 : Créer `cfg/schedule.local.toml.example`**

```toml
# Couche d'override LOCALE du planning (gitignorée — jamais écrasée par git pull).
# Copier en `cfg/schedule.local.toml` et éditer pour peaufiner EN LIVE sur le HP.
# Mêmes clés que [schedule] de general_config.toml. Hot-reload appliqué sous ~10 s.
[schedule]
# daily_match_cap = 150
# dayoff_weekdays = ["sunday"]
#
# [[schedule.pause_windows]]
# start = "12:30"
# end = "13:15"
# label = "déjeuner"
```

- [ ] **Step 3 : Ignorer la couche locale**

Ajouter à `.gitignore` (vérifier qu'elle n'y est pas déjà) :

```
cfg/schedule.local.toml
```

- [ ] **Step 4 : Vérifier que la config se charge sans erreur**

Run: `python3 -c "import play_schedule as p; s=p.PlaySchedule(); print('enabled', s.enabled, 'cap', s.daily_cap, 'windows', len(s.pause_windows_cfg))"`
Expected: affiche `enabled True cap 180 windows 0` (ou selon valeurs). Note : si `toml`/deps manquent en local, ce step se fait sur le HP en Task 7.

- [ ] **Step 5 : Commit**

```bash
git add cfg/general_config.toml cfg/schedule.local.toml.example .gitignore
git commit -m "docs(schedule): exhaustive [schedule] config + local override template"
```

---

## Task 7 : Déploiement + vérification sur le HP (manuel, opérateur)

**Files:** aucun (ops).

- [ ] **Step 1 : Lancer toute la suite de tests sur le HP** (deps réelles)

Pousser puis, dans un clone/worktree temp sur le HP :
`~/BrawlStar-Bot/venv/bin/python -m pytest tests/test_play_schedule.py -q`
Expected: tous verts.

- [ ] **Step 2 : Déclencher `git_update`** (POST `/api/instances/1/cmd` name=`git_update`) → différé fin de match.

- [ ] **Step 3 : Vérifier le hot-reload live**

Sur le HP, tail `logs/bot.log` ; après le pull, confirmer une ligne `play schedule loaded` (au prochain `get()`) avec les nouveaux champs (`pause_windows=…`, `dayoff_days=…`). Modifier `cfg/schedule.local.toml` (ex. ajouter une pause_window dans l'heure courante) → confirmer `config reloaded (mtime changed)` puis l'état `pause` sous ~10 s, et la fermeture du jeu.

- [ ] **Step 4 : Restaurer** la config live à l'état voulu (retirer la fenêtre de test).

---

## Self-Review (effectuée)

- **Couverture spec** : couches de config (Task 2,6), hot-reload (Task 2,3,4), timezone informatif (Task 3 `_apply_cfg`+`get()` log ; warning explicite optionnel), pause_windows (Task 1,3,6), overrides weekend/jour (Task 2,3,6), dayoff weekdays+chance (Task 3), block cap (Task 3), machine à états + intégration `!= play` (Task 5), fichiers config (Task 6), tests (1-4), déploiement (7). ✅
- **Placeholders** : aucun — chaque step a son code/commande.
- **Cohérence des types** : `_Window` (start_min/end_min/jitter/label, `.contains`, `.rolled`), `_resolve_day_params(base, overrides, weekday)`, `_load_layered_cfg()→(cfg, mtimes)`, états `play|break|sleep|pause|cap|dayoff` cohérents entre `state()`, `should_play_now()` et `_manage_schedule_powersave`. ✅
- **Note timezone** : le warning tz est best-effort ; si non trivial à implémenter proprement (mapping nom→offset), se contenter du log informatif dans `get()` (déjà présent). Ne pas bloquer dessus.
