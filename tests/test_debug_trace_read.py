# tests/test_debug_trace_read.py
import base64
import json
import pytest


@pytest.fixture
def dt(tmp_path, monkeypatch):
    import debug_trace as d
    monkeypatch.setattr(d, "_TRACE_DIR", tmp_path / "trace")
    monkeypatch.setattr(d, "_CAPTURE_DIR", tmp_path / "trace" / "captures")
    (tmp_path / "trace" / "captures").mkdir(parents=True)
    return d


def _write_events(dt, day, records):
    p = dt._TRACE_DIR / f"events-{day}.jsonl"
    p.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")


def test_events_missing_file_returns_empty(dt):
    out = dt.events_command({"day": "20000101"})
    assert out == {"ok": True, "count": 0, "events": []}


def test_events_tail_and_limit(dt):
    recs = [{"event": "e", "i": i} for i in range(10)]
    _write_events(dt, "20260617", recs)
    out = dt.events_command({"day": "20260617", "limit": 3})
    assert out["ok"] is True
    assert out["count"] == 3
    assert [e["i"] for e in out["events"]] == [7, 8, 9]


def test_events_skips_malformed_lines(dt):
    p = dt._TRACE_DIR / "events-20260617.jsonl"
    p.write_text('{"event":"ok"}\nNOT JSON\n{"event":"ok2"}\n', encoding="utf-8")
    out = dt.events_command({"day": "20260617"})
    assert out["count"] == 2
    assert [e["event"] for e in out["events"]] == ["ok", "ok2"]


def test_events_limit_capped_at_1000(dt):
    _write_events(dt, "20260617", [{"event": "e", "i": i} for i in range(5)])
    out = dt.events_command({"day": "20260617", "limit": 99999})
    assert out["count"] == 5  # cap doesn't error, just bounds the slice


def test_capture_valid(dt):
    raw = b"\xff\xd8\xff\xe0jpegbytes"
    (dt._CAPTURE_DIR / "brawler_read_123_1.jpg").write_bytes(raw)
    out = dt.capture_command({"name": "brawler_read_123_1.jpg"})
    assert out["ok"] is True
    assert base64.b64decode(out["jpeg_b64"]) == raw
    assert out["bytes"] == len(raw)
    assert out["name"] == "brawler_read_123_1.jpg"


def test_capture_missing_returns_error(dt):
    out = dt.capture_command({"name": "nope.jpg"})
    assert out["ok"] is False
    assert "not found" in out["error"]


@pytest.mark.parametrize("bad", ["../secret", "a/b.jpg", "..", ".hidden", "", "x" * 81, "a\\b.jpg"])
def test_capture_rejects_traversal(dt, bad):
    out = dt.capture_command({"name": bad})
    assert out["ok"] is False
    assert "invalid" in out["error"]


@pytest.mark.parametrize("bad_day", ["../evil", "2026061", "20260617x", "abcdefgh", "2026-06-17", ""])
def test_events_command_invalid_day_falls_back_to_today(dt, bad_day):
    """Non-8-digit day values must not cause path traversal — they fall back to today.
    With no today-file in the tmp dir, the result must be the empty-ok response."""
    out = dt.events_command({"day": bad_day})
    assert out == {"ok": True, "count": 0, "events": []}
