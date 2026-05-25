"""Thread-safe communication primitives between workers.

Three buses:
- `LatestSlot[T]`  : single-slot drop-old container. `set()` always wins, `get()`
                     returns the most recent value (or None if never set). Used
                     for frames and GameState where stale data is useless.
- `ControlBus`     : ordered queue of `Action` items, FIFO. Used for inputs
                     where order matters.

Workers signal each other shutdown via a shared `threading.Event` (stop_event).
"""
from __future__ import annotations

import queue
import threading
from typing import Generic, TypeVar

T = TypeVar("T")


class LatestSlot(Generic[T]):
    """Single-slot atomic container. set() overwrites; get() reads (no consume)."""

    __slots__ = ("_value", "_version", "_lock", "_event")

    def __init__(self) -> None:
        self._value: T | None = None
        self._version: int = 0
        self._lock = threading.Lock()
        self._event = threading.Event()

    def set(self, value: T) -> None:
        with self._lock:
            self._value = value
            self._version += 1
        self._event.set()

    def get(self) -> T | None:
        with self._lock:
            return self._value

    def get_with_version(self) -> tuple[T | None, int]:
        with self._lock:
            return self._value, self._version

    def wait_new(self, last_seen_version: int, timeout: float | None = None) -> tuple[T | None, int]:
        """Block until a version > `last_seen_version` is published, or timeout.

        Returns the (value, version) at the time of waking. If timeout expires
        and nothing newer arrived, returns the latest (which may equal the
        seen version).
        """
        deadline = None if timeout is None else (threading.TIMEOUT_MAX if timeout < 0 else timeout)
        # We can't atomically check + wait on a threading.Event without a small
        # spin; use a short poll loop.
        import time as _time
        start = _time.monotonic()
        while True:
            value, version = self.get_with_version()
            if version > last_seen_version:
                return value, version
            if deadline is not None and (_time.monotonic() - start) >= deadline:
                return value, version
            # Wait briefly for next set(); 10ms granularity keeps it responsive
            # — important for end-to-end latency (frame→state→action).
            self._event.wait(timeout=0.01)
            self._event.clear()

    def clear(self) -> None:
        with self._lock:
            self._value = None
        self._event.clear()


class ControlBus:
    """FIFO queue of Action items, thread-safe."""

    def __init__(self, maxsize: int = 64) -> None:
        self._q: queue.Queue = queue.Queue(maxsize=maxsize)

    def put(self, action, block: bool = True, timeout: float | None = None) -> None:
        self._q.put(action, block=block, timeout=timeout)

    def get(self, block: bool = True, timeout: float | None = None):
        return self._q.get(block=block, timeout=timeout)

    def task_done(self) -> None:
        self._q.task_done()

    def qsize(self) -> int:
        return self._q.qsize()

    def empty(self) -> bool:
        return self._q.empty()
