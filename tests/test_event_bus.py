"""EventBus replay-since for SSE resume."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest


@pytest.fixture
def bus():
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "cloud_panel"))
    # Make sure asyncio is available in this thread.
    import asyncio
    try:
        asyncio.get_event_loop()
    except RuntimeError:
        asyncio.set_event_loop(asyncio.new_event_loop())
    # Re-import fresh class (don't reuse module-level BUS).
    if "ws" in sys.modules:
        sys.modules.pop("ws")
    import ws
    return ws.EventBus()


def test_publish_assigns_monotonic_ids(bus):
    bus.publish({"type": "a"})
    bus.publish({"type": "b"})
    bus.publish({"type": "c"})
    assert [e["_id"] for e in bus._backlog] == [1, 2, 3]
    assert [e["type"] for e in bus._backlog] == ["a", "b", "c"]


def test_replay_since_returns_only_newer(bus):
    for n in "abcdef":
        bus.publish({"type": n})
    # Client last saw id=3 → expect d, e, f.
    missed = bus.replay_since(3)
    assert [e["type"] for e in missed] == ["d", "e", "f"]


def test_replay_since_when_caught_up(bus):
    bus.publish({"type": "x"})
    assert bus.replay_since(10) == []


def test_backlog_caps_at_size(bus):
    for n in range(bus.BACKLOG + 50):
        bus.publish({"type": "x", "n": n})
    assert len(bus._backlog) == bus.BACKLOG
    # Oldest should have been evicted.
    assert bus._backlog[0]["_id"] == 51
