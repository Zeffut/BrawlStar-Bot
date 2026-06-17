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
