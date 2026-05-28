"""WebSocket channel between cloud panel and worker bots.

Each worker (one per host machine) opens a persistent WebSocket to the
cloud at `/ws/worker?token=…&instance_id=…`. The connection stays open
for the lifetime of the worker, with automatic reconnects on the worker
side.

Two message flows:

  Cloud → Worker (commands):
    {"type": "cmd", "id": "<uuid>", "name": "screenshot", "args": {...}}

  Worker → Cloud:
    {"type": "cmd_result", "id": "<uuid>", "ok": true, "data": {...}}
    {"type": "log",        "lines": ["…", "…"]}
    {"type": "health",     "battery": 87, "ram_free_mb": 1234, ...}
    {"type": "screenshot", "png_b64": "<base64>"}

The cloud panel keeps the last screenshot, the last health snapshot and
a ring buffer of recent log lines per instance so the dashboard can
render them.
"""
from __future__ import annotations

import asyncio
import base64
import collections
import json
import logging
import os
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from fastapi import WebSocket, WebSocketDisconnect

log = logging.getLogger(__name__)

AUTH_TOKEN = os.environ.get("CLOUD_AUTH_TOKEN", "change-me")

# Ring-buffer sizes per instance.
LOG_BUFFER = 200      # last 200 log lines kept
SCREENSHOT_TTL = 30   # screenshot considered fresh for 30s


@dataclass
class WorkerConnection:
    instance_id: str
    ws: WebSocket
    # in-flight command futures keyed by command id
    pending: dict[str, asyncio.Future] = field(default_factory=dict)
    # last health snapshot pushed by the worker
    health: dict = field(default_factory=dict)
    # last screenshot — base64 image + mime type + epoch
    last_screenshot_b64: str | None = None
    last_screenshot_mime: str = "image/jpeg"
    last_screenshot_at: float = 0.0
    # last game snapshot {state, trophies, account_tag, ts}
    last_snapshot: dict = field(default_factory=dict)
    # ring buffer of recent log lines
    logs: collections.deque = field(default_factory=lambda: collections.deque(maxlen=LOG_BUFFER))
    connected_at: float = field(default_factory=time.time)


# ---- SSE broadcaster ----------------------------------------------

class EventBus:
    """Fan-out events to all connected browser SSE clients.

    Each published event gets a monotonically-increasing id. The bus
    keeps the last N events so reconnecting browsers (via the
    Last-Event-ID header) can resume without missing anything that
    happened during the disconnect window.
    """

    BACKLOG = 200  # ring-buffer size — last 200 events kept

    def __init__(self) -> None:
        self._subs: set[asyncio.Queue] = set()
        self._next_id = 1
        self._backlog: collections.deque = collections.deque(maxlen=self.BACKLOG)

    async def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=100)
        self._subs.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self._subs.discard(q)

    def publish(self, event: dict) -> None:
        # Tag with id; clients use it to resume after disconnects.
        eid = self._next_id
        self._next_id += 1
        tagged = {"_id": eid, **event}
        self._backlog.append(tagged)
        dead = []
        for q in self._subs:
            try:
                q.put_nowait(tagged)
            except asyncio.QueueFull:
                dead.append(q)
        for q in dead:
            self._subs.discard(q)

    def replay_since(self, last_id: int) -> list[dict]:
        """Return events newer than last_id (in order)."""
        return [e for e in self._backlog if e.get("_id", 0) > last_id]

    @property
    def count(self) -> int:
        return len(self._subs)


BUS = EventBus()


class WorkerHub:
    """Tracks active worker connections keyed by instance_id."""

    def __init__(self) -> None:
        self._conns: dict[str, WorkerConnection] = {}
        self._lock = asyncio.Lock()

    async def register(self, instance_id: str, ws: WebSocket) -> WorkerConnection:
        async with self._lock:
            # If an old connection lingered for this instance, close it.
            old = self._conns.get(instance_id)
            if old is not None:
                log.info("replacing existing connection for %s", instance_id)
                try:
                    await old.ws.close(code=1000, reason="superseded")
                except Exception:
                    pass
            conn = WorkerConnection(instance_id=instance_id, ws=ws)
            self._conns[instance_id] = conn
            log.info("worker connected: %s (total=%d)", instance_id, len(self._conns))
        # Notify browsers (outside the lock to avoid re-entrant issues).
        BUS.publish({"type": "instance_connected", "instance_id": instance_id})
        # NOTE: no notif dispatch on instance_connected — self-updates,
        # service restarts and code redeploys all trigger reconnects,
        # so Telegram would receive a boot ping for every push. The
        # disconnect notif remains because it indicates a real anomaly.
        return conn

    async def unregister(self, conn: WorkerConnection) -> None:
        async with self._lock:
            cur = self._conns.get(conn.instance_id)
            if cur is conn:
                del self._conns[conn.instance_id]
            log.info("worker disconnected: %s (total=%d)",
                     conn.instance_id, len(self._conns))
            # Cancel every pending command future so callers don't hang.
            for fut in conn.pending.values():
                if not fut.done():
                    fut.set_exception(ConnectionError("worker disconnected"))
        BUS.publish({"type": "instance_disconnected",
                     "instance_id": conn.instance_id})
        try:
            import notif as _notif
            _notif.dispatch("instance_disconnected", {"instance_id": conn.instance_id})
        except Exception:
            log.exception("notif dispatch instance_disconnected failed")

    def get(self, instance_id: str) -> WorkerConnection | None:
        return self._conns.get(instance_id)

    def list(self) -> list[WorkerConnection]:
        return list(self._conns.values())

    async def send_command(self, instance_id: str, name: str,
                           args: dict | None = None, timeout_s: float = 10.0) -> Any:
        """Send a command to a worker and wait for its reply."""
        conn = self.get(instance_id)
        if conn is None:
            raise ConnectionError(f"worker {instance_id} not connected")
        cmd_id = str(uuid.uuid4())
        loop = asyncio.get_running_loop()
        fut: asyncio.Future = loop.create_future()
        conn.pending[cmd_id] = fut
        msg = {"type": "cmd", "id": cmd_id, "name": name, "args": args or {}}
        try:
            await conn.ws.send_text(json.dumps(msg))
        except Exception as exc:
            conn.pending.pop(cmd_id, None)
            raise ConnectionError(f"send failed: {exc}") from exc
        try:
            return await asyncio.wait_for(fut, timeout=timeout_s)
        except asyncio.TimeoutError:
            conn.pending.pop(cmd_id, None)
            raise TimeoutError(f"command {name} timed out after {timeout_s}s")


HUB = WorkerHub()


async def worker_ws_endpoint(ws: WebSocket, token: str = "", instance_id: str = "") -> None:
    """FastAPI WebSocket route handler (registered in cloud_panel/app.py)."""
    if token != AUTH_TOKEN:
        await ws.close(code=1008, reason="bad token")
        return
    if not instance_id:
        await ws.close(code=1008, reason="missing instance_id")
        return
    await ws.accept()
    conn = await HUB.register(instance_id, ws)
    try:
        while True:
            raw = await ws.receive_text()
            try:
                msg = json.loads(raw)
            except Exception:
                log.warning("bad json from %s: %r", instance_id, raw[:200])
                continue
            mtype = msg.get("type")
            if mtype == "cmd_result":
                cmd_id = msg.get("id")
                fut = conn.pending.pop(cmd_id, None)
                if fut and not fut.done():
                    if msg.get("ok"):
                        fut.set_result(msg.get("data"))
                    else:
                        fut.set_exception(RuntimeError(msg.get("error") or "command failed"))
            elif mtype == "log":
                for line in (msg.get("lines") or []):
                    conn.logs.append({"t": time.time(), "line": line})
            elif mtype == "health":
                conn.health = msg.get("data") or {}
                conn.health["_pushed_at"] = time.time()
            elif mtype == "screenshot":
                # Accept either jpeg_b64 (new, smaller) or png_b64 (legacy).
                b64 = msg.get("jpeg_b64") or msg.get("png_b64")
                mime = "image/jpeg" if msg.get("jpeg_b64") else "image/png"
                conn.last_screenshot_b64 = b64
                conn.last_screenshot_mime = mime
                conn.last_screenshot_at = time.time()
            elif mtype == "snapshot":
                data = msg.get("data") or {}
                conn.last_snapshot = {**data, "_pushed_at": time.time()}
                # Persist to DB so the panel shows last-known data even
                # when the worker disconnects.
                try:
                    import db as _db
                    _db.save_instance_snapshot(instance_id, data)
                except Exception:
                    log.debug("snapshot persist failed", exc_info=True)
                # Broadcast to SSE subscribers (browsers).
                BUS.publish({
                    "type": "snapshot",
                    "instance_id": instance_id,
                    **data,
                })
            elif mtype == "hello":
                # informational; worker can resend on reconnect
                pass
            else:
                log.debug("unhandled msg from %s: %s", instance_id, mtype)
    except WebSocketDisconnect:
        pass
    except Exception:
        log.exception("worker ws crashed for %s", instance_id)
    finally:
        await HUB.unregister(conn)
