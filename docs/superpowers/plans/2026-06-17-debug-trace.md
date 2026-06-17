# Debug Trace System — Implementation Plan (Phase 1)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Donner au bot un logging ultra-détaillé (événements JSON structurés) + captures d'écran réutilisées aux points de décision, pour débugger petit à petit — en commençant par le bug « affiche Rico mais joue béa ».

**Architecture:** Un module `debug_trace.py` expose `trace(event, data, frame, crop, …)`. Le chemin chaud ne fait qu'**enqueue** ; un **thread writer daemon** encode le JPEG et écrit le JSONL. Les captures **réutilisent une frame déjà décodée** (stream `get_frame()` ou frame passée) et **n'appellent jamais `adb screencap`**. On instrumente 5 points (lecture brawler, réconciliation, fin de match, recoveries goto_lobby).

**Tech Stack:** Python 3, stdlib (`json`, `queue`, `threading`, `logging`, `pathlib`), `PIL`/`cv2`/`numpy` (déjà présents), `pytest`.

## Global Constraints

- **Ne JAMAIS toucher la résolution du stream `screen_capture` partagé** (alimente live panel + vision bot, `state_finder` calibré 1280).
- **Captures = frame déjà en mémoire uniquement** (`screen_capture.get().get_frame()` ou frame passée). **Interdiction d'appeler `_adb_screencap()` / `adb screencap` dans tout chemin de trace.**
- **Hors chemin chaud** : `trace()` enqueue seulement ; queue bornée `maxsize=32`, **drop-oldest** si saturée.
- **Best-effort total** : toute exception dans `trace()` ou le writer est avalée — le bot n'est jamais impacté.
- **Rétention bornée** : captures cap `TRACE_CAPTURE_MAX_MB` (défaut 400) → suppression des plus vieilles ; JSONL rotation quotidienne.
- **Config env** : `BOT_DEBUG_TRACE` = `off`|`on`(défaut)|`verbose` ; `TRACE_CAPTURE_MIN_INTERVAL_S` (défaut 3.0) ; `TRACE_CAPTURE_MAX_MB` (défaut 400).
- **Couleurs** : les ndarray passés à l'encodage sont en **RGB** ; `debug_trace` convertit la frame stream (BGR) en RGB avant de l'utiliser.
- `logs/` est gitignored — les artefacts ne sont jamais commités.
- Le **grind ne doit jamais casser** : déploiement worker validé en live (Task 6).

---

### Task 1: Module cœur `debug_trace.py`

**Files:**
- Create: `debug_trace.py`
- Test: `tests/test_debug_trace.py`

**Interfaces:**
- Produces:
  - `trace(event: str, data: dict|None=None, frame=None, crop=None, capture: bool=True, level: str="info", tag: str|None=None, force_capture: bool=False) -> None`
  - Module globals (monkeypatchables en test) : `_MODE: str`, `_MIN_INTERVAL: float`, `_MAX_BYTES: int`, `_TRACE_DIR: Path`, `_CAPTURE_DIR: Path`, `_last_capture_at: dict`
  - Helper test : `_flush(timeout: float=3.0) -> None`, `_reset_for_tests() -> None`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_debug_trace.py
import json
import numpy as np
import pytest


@pytest.fixture
def dt(tmp_path, monkeypatch):
    import debug_trace as d
    monkeypatch.setattr(d, "_TRACE_DIR", tmp_path / "trace")
    monkeypatch.setattr(d, "_CAPTURE_DIR", tmp_path / "trace" / "captures")
    monkeypatch.setattr(d, "_MODE", "on")
    monkeypatch.setattr(d, "_MIN_INTERVAL", 0.0)
    monkeypatch.setattr(d, "_MAX_BYTES", 400 * 1024 * 1024)
    d._reset_for_tests()
    return d


def _events(d):
    files = list((d._TRACE_DIR).glob("events-*.jsonl"))
    if not files:
        return []
    return [json.loads(l) for l in files[0].read_text().splitlines() if l.strip()]


def test_trace_writes_jsonl_event(dt):
    dt.trace("hello", {"a": 1}, capture=False)
    dt._flush()
    evs = _events(dt)
    assert len(evs) == 1
    assert evs[0]["event"] == "hello"
    assert evs[0]["data"] == {"a": 1}
    assert "ts" in evs[0] and "iso" in evs[0]


def test_trace_off_is_noop(dt, monkeypatch):
    monkeypatch.setattr(dt, "_MODE", "off")
    dt.trace("hello", {"a": 1}, capture=False)
    dt._flush()
    assert _events(dt) == []


def test_capture_written_from_ndarray(dt):
    frame = np.zeros((20, 30, 3), dtype=np.uint8)
    dt.trace("cap", {}, frame=frame)
    dt._flush()
    jpgs = list(dt._CAPTURE_DIR.glob("*.jpg"))
    assert len(jpgs) == 1
    assert _events(dt)[0]["capture"].endswith(".jpg")


def test_no_screencap_when_no_frame(dt, monkeypatch):
    import screen_capture
    monkeypatch.setattr(screen_capture, "get", lambda *a, **k: None)
    dt.trace("noframe", {}, frame=None, capture=True)
    dt._flush()
    evs = _events(dt)
    assert len(evs) == 1
    assert "capture" not in evs[0]
    assert list(dt._CAPTURE_DIR.glob("*.jpg")) == []


def test_throttle_limits_captures(dt, monkeypatch):
    monkeypatch.setattr(dt, "_MIN_INTERVAL", 1000.0)
    frame = np.zeros((10, 10, 3), dtype=np.uint8)
    dt.trace("ev", {}, frame=frame)
    dt.trace("ev", {}, frame=frame)
    dt._flush()
    assert len(list(dt._CAPTURE_DIR.glob("ev*.jpg"))) == 1
    assert _events(dt)[1].get("capture_throttled") is True


def test_force_capture_bypasses_throttle(dt, monkeypatch):
    monkeypatch.setattr(dt, "_MIN_INTERVAL", 1000.0)
    frame = np.zeros((10, 10, 3), dtype=np.uint8)
    dt.trace("ev", {}, frame=frame, force_capture=True)
    dt.trace("ev", {}, frame=frame, force_capture=True)
    dt._flush()
    assert len(list(dt._CAPTURE_DIR.glob("ev*.jpg"))) == 2


def test_retention_deletes_oldest(dt, monkeypatch):
    monkeypatch.setattr(dt, "_MAX_BYTES", 1)  # any capture trips retention
    frame = np.zeros((10, 10, 3), dtype=np.uint8)
    dt.trace("a", {}, frame=frame, force_capture=True)
    dt._flush()
    dt.trace("b", {}, frame=frame, force_capture=True)
    dt._flush()
    # retention keeps total under cap → at most the newest survives
    assert len(list(dt._CAPTURE_DIR.glob("*.jpg"))) <= 1
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_debug_trace.py -v`
Expected: FAIL (`ModuleNotFoundError: No module named 'debug_trace'`)

- [ ] **Step 3: Write the module**

```python
# debug_trace.py
"""Ultra-detailed debug tracing: structured JSONL events + reused-frame captures.

Every decision point can emit `trace(event, data=..., frame=..., crop=...)`. The
hot path only ENQUEUES; a daemon writer thread does the JPEG encode + disk write.
Captures REUSE an already-decoded frame (the live stream's get_frame() or a
caller-passed frame) — they NEVER trigger a fresh `adb screencap` (that contends
with the shared screenrecord and stalls the live feed / breaks the grind).

Best-effort: any exception is swallowed; the bot is never impacted.

Config (env):
    BOT_DEBUG_TRACE = off | on (default) | verbose
    TRACE_CAPTURE_MIN_INTERVAL_S (default 3.0)
    TRACE_CAPTURE_MAX_MB (default 400)
"""
from __future__ import annotations

import json
import logging
import os
import queue
import threading
import time
from pathlib import Path

log = logging.getLogger("trace")

_TRACE_DIR = Path(__file__).resolve().parent / "logs" / "trace"
_CAPTURE_DIR = _TRACE_DIR / "captures"


def _envf(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except Exception:
        return float(default)


_MODE = (os.environ.get("BOT_DEBUG_TRACE", "on") or "on").strip().lower()
_MIN_INTERVAL = _envf("TRACE_CAPTURE_MIN_INTERVAL_S", 3.0)
_MAX_BYTES = int(_envf("TRACE_CAPTURE_MAX_MB", 400) * 1024 * 1024)

_Q: "queue.Queue | None" = None
_WRITER: "threading.Thread | None" = None
_START_LOCK = threading.Lock()
_last_capture_at: dict[str, float] = {}


def _safe(s) -> str:
    return "".join(c if (c.isalnum() or c in "-_") else "_" for c in str(s))[:40]


def _summary(data) -> str:
    if not data:
        return ""
    try:
        return " ".join(f"{k}={v}" for k, v in list(data.items())[:6])
    except Exception:
        return ""


def _account():
    try:
        import device
        tag, _ = device.account_override()
        return tag
    except Exception:
        return None


def _latest_stream_frame():
    """Latest decoded stream frame as an RGB ndarray, or None. Never screencaps."""
    try:
        import cv2
        import screen_capture
        rec = screen_capture.get()
        if rec is None:
            return None
        f = rec.get_frame()
        age = rec.get_frame_age()
        if f is not None and age is not None and age < 6.0:
            return cv2.cvtColor(f, cv2.COLOR_BGR2RGB)
    except Exception:
        pass
    return None


def _ensure_writer() -> None:
    global _Q, _WRITER
    if _WRITER is not None and _WRITER.is_alive():
        return
    with _START_LOCK:
        if _WRITER is not None and _WRITER.is_alive():
            return
        _TRACE_DIR.mkdir(parents=True, exist_ok=True)
        _CAPTURE_DIR.mkdir(parents=True, exist_ok=True)
        _Q = queue.Queue(maxsize=32)
        _WRITER = threading.Thread(target=_writer_loop, name="debug-trace", daemon=True)
        _WRITER.start()


def trace(event, data=None, frame=None, crop=None, capture=True,
          level="info", tag=None, force_capture=False) -> None:
    """Record a debug event (+ optional reused-frame capture). Best-effort."""
    if _MODE == "off":
        return
    try:
        try:
            lvl = level if level in ("debug", "info", "warning", "error") else "info"
            getattr(log, lvl)("%s %s", event, _summary(data))
        except Exception:
            pass
        if capture and frame is None:
            frame = _latest_stream_frame()  # reused only; never a screencap
        rec = {
            "ts": int(time.time() * 1000),
            "iso": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "event": event,
            "tag": tag,
            "account": _account(),
            "data": data or {},
        }
        _ensure_writer()
        item = (rec, frame if capture else None, crop if capture else None,
                bool(force_capture))
        try:
            _Q.put_nowait(item)
        except queue.Full:
            try:
                _Q.get_nowait()
                _Q.task_done()
                _Q.put_nowait(item)
            except Exception:
                pass
    except Exception:
        try:
            log.debug("trace() failed", exc_info=True)
        except Exception:
            pass


def _write_jpeg(img, path: Path, quality: int = 70) -> bool:
    try:
        import numpy as np
        from PIL import Image
        pil = Image.fromarray(img) if isinstance(img, np.ndarray) else img
        pil.convert("RGB").save(str(path), format="JPEG", quality=quality)
        return True
    except Exception:
        try:
            log.debug("trace jpeg write failed", exc_info=True)
        except Exception:
            pass
        return False


def _append_jsonl(rec: dict) -> None:
    day = time.strftime("%Y%m%d")
    path = _TRACE_DIR / f"events-{day}.jsonl"
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")


def _enforce_retention() -> None:
    try:
        files = sorted(_CAPTURE_DIR.glob("*.jpg"), key=lambda p: p.stat().st_mtime)
        total = sum(p.stat().st_size for p in files)
        i = 0
        while total > _MAX_BYTES and i < len(files):
            try:
                sz = files[i].stat().st_size
                files[i].unlink(missing_ok=True)
                total -= sz
            except Exception:
                pass
            i += 1
    except Exception:
        pass


def _writer_loop() -> None:
    while True:
        try:
            rec, frame, crop, force = _Q.get()
        except Exception:
            continue
        try:
            cap_name = None
            if frame is not None:
                ev = rec["event"]
                now = time.time()
                throttled = (not force and _MODE != "verbose"
                             and (now - _last_capture_at.get(ev, 0.0)) < _MIN_INTERVAL)
                if throttled:
                    rec["capture_throttled"] = True
                else:
                    cap_name = f"{_safe(ev)}_{rec['ts']}.jpg"
                    if _write_jpeg(frame, _CAPTURE_DIR / cap_name):
                        rec["capture"] = cap_name
                        _last_capture_at[ev] = now
                        if crop is not None:
                            crop_name = f"{_safe(ev)}_{rec['ts']}.crop.jpg"
                            if _write_jpeg(crop, _CAPTURE_DIR / crop_name):
                                rec["crop"] = crop_name
                    else:
                        cap_name = None
            _append_jsonl(rec)
            if cap_name is not None:
                _enforce_retention()
        except Exception:
            try:
                log.debug("trace writer record failed", exc_info=True)
            except Exception:
                pass
        finally:
            try:
                _Q.task_done()
            except Exception:
                pass


def _flush(timeout: float = 3.0) -> None:
    """Block until queued records are written (test helper only)."""
    deadline = time.time() + timeout
    while _Q is not None and not _Q.empty() and time.time() < deadline:
        time.sleep(0.02)
    time.sleep(0.15)  # let the in-flight record finish writing


def _reset_for_tests() -> None:
    """Clear throttle memory between tests (test helper only)."""
    _last_capture_at.clear()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_debug_trace.py -v`
Expected: PASS (7 tests)

- [ ] **Step 5: Commit**

```bash
git add debug_trace.py tests/test_debug_trace.py
git commit -m "feat(debug): debug_trace core — structured JSONL events + reused-frame captures"
```

---

### Task 2: Instrumenter `read_current_brawler` (le bug rico/bea)

**Files:**
- Modify: `game_api.py:645-660` (`read_current_brawler`)
- Test: `tests/test_trace_read_brawler.py`

**Interfaces:**
- Consumes: `debug_trace.trace(...)` (Task 1)
- Produces: event `"brawler_read"` avec `data={"ocr_raw": [...], "token": <str|None>}`, `frame=arr` (RGB), `crop=crop`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_trace_read_brawler.py
from PIL import Image


def test_read_current_brawler_emits_trace(monkeypatch):
    import game_api
    import debug_trace
    api = game_api.GameAPI(None, None)
    monkeypatch.setattr(api, "_grab", lambda: Image.new("RGB", (1280, 576)))
    monkeypatch.setattr(game_api, "extract_text_and_positions",
                        lambda crop: {"rico": (0, 0)})
    calls = []
    monkeypatch.setattr(debug_trace, "trace", lambda *a, **k: calls.append((a, k)))
    out = api.read_current_brawler()
    assert out == "rico"
    assert calls and calls[0][0][0] == "brawler_read"
    assert calls[0][1]["data"]["token"] == "rico"
    assert "rico" in calls[0][1]["data"]["ocr_raw"]
    assert calls[0][1]["crop"] is not None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_trace_read_brawler.py -v`
Expected: FAIL (no `"brawler_read"` trace emitted)

- [ ] **Step 3: Modify `read_current_brawler`**

Replace the body (`game_api.py:645-660`) with:

```python
    def read_current_brawler(self) -> str | None:
        """OCR the brawler name shown above the play button in lobby."""
        try:
            img = self._grab()
            arr = np.array(img)
            h, w = arr.shape[:2]
            # The current brawler name is shown center-bottom under the avatar.
            crop = arr[int(h * 0.72):int(h * 0.82), int(w * 0.35):int(w * 0.65)]
            text = extract_text_and_positions(crop)
            chosen = None
            for key in text.keys():
                key = key.strip()
                if key.isalpha() and 3 <= len(key) <= 16:
                    chosen = key.lower()
                    break
            try:
                import debug_trace
                debug_trace.trace(
                    "brawler_read",
                    data={"ocr_raw": list(text.keys()), "token": chosen},
                    frame=arr, crop=crop, level="debug",
                )
            except Exception:
                pass
            return chosen
        except Exception as exc:
            log.warning("read_current_brawler(): %s", exc)
        return None
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_trace_read_brawler.py tests/test_debug_trace.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add game_api.py tests/test_trace_read_brawler.py
git commit -m "feat(debug): trace brawler_read (OCR raw + token + crop) — diagnose rico/bea"
```

---

### Task 3: Réconciliation brawler — extraire `_reconcile_equipped_brawler` + trace

**Files:**
- Modify: `stage_manager.py:166-182` (le bloc reconcile inline dans `start_game`)
- Modify: `stage_manager.py` (ajouter la méthode `_reconcile_equipped_brawler`)
- Test: `tests/test_trace_reconcile.py`

**Interfaces:**
- Consumes: `debug_trace.trace(...)`, `game_api.get()`, `lobby_automation.resolve_equipped_to_canonical(ocr, candidates)`
- Produces: méthode `StageManager._reconcile_equipped_brawler(self) -> str` ; event `"brawler_reconcile"` avec `data={"intended","equipped_ocr","canonical","corrected","final"}`, `force_capture=True`. Cet event sert AUSSI de marqueur « match start » (le brawler définitif avant PLAY).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_trace_reconcile.py
def test_reconcile_corrects_and_traces(monkeypatch):
    import stage_manager
    import game_api
    import lobby_automation
    import debug_trace
    sm = stage_manager.StageManager.__new__(stage_manager.StageManager)
    sm.brawlers_pick_data = [{"brawler": "rico"}]
    sm._owned_brawler_names = ["bea", "rico"]

    class FakeAPI:
        def read_current_brawler(self):
            return "bea"

    monkeypatch.setattr(game_api, "get", lambda: FakeAPI())
    monkeypatch.setattr(lobby_automation, "resolve_equipped_to_canonical",
                        lambda ocr, cands: "bea")
    calls = []
    monkeypatch.setattr(debug_trace, "trace", lambda *a, **k: calls.append((a, k)))

    final = sm._reconcile_equipped_brawler()
    assert final == "bea"
    assert sm.brawlers_pick_data[0]["brawler"] == "bea"
    assert calls[0][0][0] == "brawler_reconcile"
    d = calls[0][1]["data"]
    assert d["intended"] == "rico" and d["corrected"] is True and d["final"] == "bea"
    assert calls[0][1]["force_capture"] is True


def test_reconcile_keeps_intended_when_unreadable(monkeypatch):
    import stage_manager, game_api, debug_trace
    sm = stage_manager.StageManager.__new__(stage_manager.StageManager)
    sm.brawlers_pick_data = [{"brawler": "rico"}]
    sm._owned_brawler_names = ["bea", "rico"]

    class FakeAPI:
        def read_current_brawler(self):
            return None

    monkeypatch.setattr(game_api, "get", lambda: FakeAPI())
    monkeypatch.setattr(debug_trace, "trace", lambda *a, **k: None)
    assert sm._reconcile_equipped_brawler() == "rico"
    assert sm.brawlers_pick_data[0]["brawler"] == "rico"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_trace_reconcile.py -v`
Expected: FAIL (`AttributeError: ... has no attribute '_reconcile_equipped_brawler'`)

- [ ] **Step 3: Add the method**

Add this method to `class StageManager` (e.g. right before `start_game`):

```python
    def _reconcile_equipped_brawler(self) -> str:
        """Reconcile the recorded brawler with what's ACTUALLY equipped (OCR above
        the PLAY button) before tapping PLAY, and trace the decision. Returns the
        final brawler name that will be recorded. Best-effort: any failure keeps
        the intended name. The emitted "brawler_reconcile" event also marks the
        match start (the definitive brawler before PLAY)."""
        intended = self.brawlers_pick_data[0]['brawler']
        equipped_ocr = None
        canonical = None
        corrected = False
        try:
            import game_api
            from lobby_automation import resolve_equipped_to_canonical
            api = game_api.get()
            equipped_ocr = api.read_current_brawler() if api else None
            roster = getattr(self, "_owned_brawler_names", None)
            canonical = (resolve_equipped_to_canonical(equipped_ocr, roster)
                         if equipped_ocr else None)
            if canonical and canonical.strip().lower() != (intended or "").strip().lower():
                log.warning("brawler reconcile: intended=%r but equipped reads %r → "
                            "recording/labelling as %r", intended, equipped_ocr, canonical)
                self.brawlers_pick_data[0]['brawler'] = canonical
                corrected = True
        except Exception:
            log.debug("brawler reconcile failed", exc_info=True)
        final = self.brawlers_pick_data[0]['brawler']
        try:
            import debug_trace
            debug_trace.trace(
                "brawler_reconcile",
                data={"intended": intended, "equipped_ocr": equipped_ocr,
                      "canonical": canonical, "corrected": corrected, "final": final},
                force_capture=True, level="info",
            )
        except Exception:
            pass
        return final
```

- [ ] **Step 4: Replace the inline reconcile block in `start_game`**

Replace the existing inline block (`stage_manager.py:166-182`, the `try: … brawler reconcile … except Exception: log.debug("brawler reconcile failed", exc_info=True)`) with a single call:

```python
        # Reconcile the recorded brawler with what's ACTUALLY equipped (and trace
        # the decision) before we tap PLAY. See _reconcile_equipped_brawler.
        self._reconcile_equipped_brawler()
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `python -m pytest tests/test_trace_reconcile.py -v`
Expected: PASS (2 tests)

- [ ] **Step 6: Commit**

```bash
git add stage_manager.py tests/test_trace_reconcile.py
git commit -m "feat(debug): trace brawler_reconcile (intended/equipped/canonical) + extract method"
```

---

### Task 4: Trace fin de match

**Files:**
- Modify: `stage_manager.py` (ajouter `_trace_match_result`)
- Modify: `stage_manager.py:351-354` (`end_game`, après `find_game_result`)
- Test: `tests/test_trace_match_end.py`

**Interfaces:**
- Consumes: `debug_trace.trace(...)`
- Produces: méthode `StageManager._trace_match_result(self, result) -> None` ; event `"match_end"` avec `data={"brawler","result"}`, `force_capture=True`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_trace_match_end.py
def test_trace_match_result(monkeypatch):
    import stage_manager
    import debug_trace
    sm = stage_manager.StageManager.__new__(stage_manager.StageManager)
    sm.brawlers_pick_data = [{"brawler": "bea"}]
    calls = []
    monkeypatch.setattr(debug_trace, "trace", lambda *a, **k: calls.append((a, k)))
    sm._trace_match_result("victory")
    assert calls and calls[0][0][0] == "match_end"
    assert calls[0][1]["data"]["brawler"] == "bea"
    assert calls[0][1]["data"]["result"] == "victory"
    assert calls[0][1]["force_capture"] is True
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_trace_match_end.py -v`
Expected: FAIL (`AttributeError: ... '_trace_match_result'`)

- [ ] **Step 3: Add the method**

Add to `class StageManager`:

```python
    def _trace_match_result(self, result) -> None:
        """Trace a finished match's result (brawler + outcome). Best-effort."""
        try:
            import debug_trace
            brawler = None
            try:
                brawler = self.brawlers_pick_data[0].get('brawler')
            except Exception:
                pass
            debug_trace.trace(
                "match_end", data={"brawler": brawler, "result": result},
                force_capture=True, level="info",
            )
        except Exception:
            pass
```

- [ ] **Step 4: Call it in `end_game`**

In `end_game`, right after the `save_brawler_data(self.brawlers_pick_data)` line (`stage_manager.py:354`), add:

```python
                if found_game_result:
                    self._trace_match_result(found_game_result)
```

(Same indentation as `save_brawler_data` — inside the `if not found_game_result and …:` block, so it fires the iteration the result is found.)

- [ ] **Step 5: Run tests to verify they pass**

Run: `python -m pytest tests/test_trace_match_end.py -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add stage_manager.py tests/test_trace_match_end.py
git commit -m "feat(debug): trace match_end (brawler + result)"
```

---

### Task 5: Trace recoveries `goto_lobby` + restart BS

**Files:**
- Modify: `game_api.py:664-693` (`_restart_brawlstars`)
- Modify: `game_api.py:778-781` (branche quit-dialog) et `game_api.py:823-827` (branche home button)
- Test: `tests/test_trace_recovery.py`

**Interfaces:**
- Consumes: `debug_trace.trace(...)`
- Produces: event `"bs_restart"` (`data={"reason"}`, `force_capture=True`, `level="warning"`) ; event `"goto_recovery"` (`data={"action","state"?,"iter"}`).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_trace_recovery.py
def test_restart_brawlstars_traces(monkeypatch):
    import game_api
    import debug_trace
    import device
    api = game_api.GameAPI(None, None)
    api._last_restart_t = 0.0
    monkeypatch.setattr(game_api.time, "time", lambda: 10_000.0)
    monkeypatch.setattr(game_api.time, "sleep", lambda *a, **k: None)
    monkeypatch.setattr(device, "adb_serial", lambda: "X")
    monkeypatch.setattr(game_api.subprocess, "run", lambda *a, **k: None)
    calls = []
    monkeypatch.setattr(debug_trace, "trace", lambda *a, **k: calls.append((a, k)))
    api._restart_brawlstars("test reason")
    assert calls and calls[0][0][0] == "bs_restart"
    assert calls[0][1]["data"]["reason"] == "test reason"


def test_restart_suppressed_does_not_trace(monkeypatch):
    import game_api, debug_trace
    api = game_api.GameAPI(None, None)
    api._last_restart_t = 9_999.0  # very recent
    monkeypatch.setattr(game_api.time, "time", lambda: 10_000.0)  # <45s later
    monkeypatch.setattr(game_api.time, "sleep", lambda *a, **k: None)
    calls = []
    monkeypatch.setattr(debug_trace, "trace", lambda *a, **k: calls.append((a, k)))
    api._restart_brawlstars("suppressed")
    assert calls == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_trace_recovery.py -v`
Expected: FAIL (no `"bs_restart"` trace)

- [ ] **Step 3: Add the trace in `_restart_brawlstars`**

In `_restart_brawlstars`, AFTER `self._last_restart_t = now` and BEFORE `serial = device.adb_serial()`, insert:

```python
        try:
            import debug_trace
            debug_trace.trace("bs_restart", data={"reason": reason},
                              force_capture=True, level="warning")
        except Exception:
            pass
```

(Placing it after the cooldown check guarantees suppressed restarts don't trace.)

- [ ] **Step 4: Add the goto_lobby recovery traces**

In `_goto_lobby_impl`, in the quit-dialog branch (`if self._dismiss_quit_dialog():`, after its `log.info(...)`), add:

```python
                try:
                    import debug_trace
                    debug_trace.trace("goto_recovery",
                                      data={"action": "quit_dialog_cancel", "iter": i})
                except Exception:
                    pass
```

And in the `if st in ("brawler_selection", "shop"):` branch (after its `log.info(...)`), add:

```python
                try:
                    import debug_trace
                    debug_trace.trace("goto_recovery",
                                      data={"action": "home_button", "state": st, "iter": i})
                except Exception:
                    pass
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `python -m pytest tests/test_trace_recovery.py -v`
Expected: PASS (2 tests)

- [ ] **Step 6: Run the full trace test suite + commit**

Run: `python -m pytest tests/test_debug_trace.py tests/test_trace_read_brawler.py tests/test_trace_reconcile.py tests/test_trace_match_end.py tests/test_trace_recovery.py -v`
Expected: PASS (all)

```bash
git add game_api.py tests/test_trace_recovery.py
git commit -m "feat(debug): trace bs_restart + goto_lobby recoveries"
```

---

### Task 6: Déployer Phase 1 sur le worker + vérifier en live (grind intact + bug rico/bea)

**Files:** aucun (déploiement + vérification)

**Interfaces:**
- Consumes: tous les events ajoutés (`brawler_read`, `brawler_reconcile`, `match_end`, `bs_restart`, `goto_recovery`).

- [ ] **Step 1: Push main**

```bash
git push origin main
```
Expected: push OK (les commits Task 1-5 sont sur `origin/main`).

- [ ] **Step 2: Déployer sur le HP worker** (device.toml untracked, préservé)

```bash
ssh -p 2222 <hp> 'cd ~/BrawlStar-Bot && git fetch origin && git reset --hard origin/main && sudo systemctl restart brawlbot'
```
(Identifiants exacts via `~/.claude/secrets.env` / mémoire `reference_credentials` — ne pas les afficher.)
Expected: `HEAD now at <sha> feat(debug): trace bs_restart…`, service redémarré.

- [ ] **Step 3: Vérifier que le grind tourne toujours** (~2-3 min d'observation)

```bash
ssh -p 2222 <hp> 'journalctl -u brawlbot -n 60 --no-pager'
```
Expected: lobby atteint, `screenrec spawn …1280x576…`, matchs qui s'enchaînent, **0 traceback**. Si cassé → `git reset --hard <sha avant>` + restart, et stop.

- [ ] **Step 4: Vérifier que les events sont écrits**

```bash
ssh -p 2222 <hp> 'tail -n 20 ~/BrawlStar-Bot/logs/trace/events-$(date +%Y%m%d).jsonl; ls -la ~/BrawlStar-Bot/logs/trace/captures/ | tail'
```
Expected: lignes JSON `brawler_read` / `brawler_reconcile` / `match_end`, et des `.jpg` dans `captures/`.

- [ ] **Step 5: Trancher le bug rico/bea**

```bash
ssh -p 2222 <hp> 'grep -h brawler_reconcile ~/BrawlStar-Bot/logs/trace/events-$(date +%Y%m%d).jsonl | tail -5'
# puis récupérer la capture correspondante :
scp -P 2222 <hp>:~/BrawlStar-Bot/logs/trace/captures/brawler_reconcile_<ts>.jpg /tmp/
```
Comparer la capture (brawler réellement équipé à l'écran) au champ `equipped_ocr`/`token` :
- Si `equipped_ocr` lit `rico` alors que la capture montre `bea` → **bug OCR** dans `read_current_brawler` (région/préproc) → ouvrir un suivi.
- Si la capture montre réellement `rico` → le label est correct ; expliquer à Zeffut.

- [ ] **Step 6: Vérifier qu'aucun screencap n'a été ajouté au chemin de trace**

```bash
grep -n "_adb_screencap\|screencap" debug_trace.py
```
Expected: aucune occurrence (la garantie de non-contention tient).

---

## Self-Review

**Spec coverage:**
- §3.1 module cœur (queue bornée drop-oldest, writer thread, miroir humain, throttle, rétention, frame réutilisée) → Task 1. ✓
- §3.2 config env (`BOT_DEBUG_TRACE`/intervalle/cap) → Task 1 (globals + `_MODE`). ✓
- §3.3 points instrumentés : `read_current_brawler` → Task 2 ; reconcile + match-start → Task 3 (consolidés en `brawler_reconcile`) ; match-end → Task 4 ; recoveries goto_lobby (restart/quit/home) → Task 5. ✓
- §4 robustesse best-effort → try/except partout + tests off/no-frame. ✓
- §5 tests (jsonl valide, throttle, rétention, frame=None sans screencap, off=noop, garde-fou screencap) → Task 1 + Task 6 step 6. ✓
- §7 découpage : Phase 1 livrée + vérifiée (Task 6) avant Phase 2 (hors de ce plan). ✓

**Placeholder scan:** aucun TBD/TODO ; le module (writer + helpers compris) est fourni complet en un seul Step 3, code prêt à coller. ✓

**Type consistency:** `trace(event, data, frame, crop, capture, level, tag, force_capture)` — signature identique partout ; events nommés de façon cohérente (`brawler_read`, `brawler_reconcile`, `match_end`, `bs_restart`, `goto_recovery`) ; `_reconcile_equipped_brawler`/`_trace_match_result` référencés tels que définis. ✓

**Phase 2 (hors périmètre de ce plan) :** routes `worker_link` `/api/debug/events` + `/api/debug/capture/{name}`, proxy cloud panel, page `/debug`. À planifier séparément après validation Task 6.
