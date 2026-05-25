"""Thread-safety + behavior tests for buses."""
from __future__ import annotations

import threading
import time

import pytest

from bsbot.buses import ControlBus, LatestSlot


class TestLatestSlot:
    def test_empty_returns_none(self):
        slot: LatestSlot[int] = LatestSlot()
        assert slot.get() is None

    def test_set_then_get(self):
        slot: LatestSlot[str] = LatestSlot()
        slot.set("hello")
        assert slot.get() == "hello"

    def test_overwrite_keeps_latest(self):
        slot: LatestSlot[int] = LatestSlot()
        for i in range(100):
            slot.set(i)
        assert slot.get() == 99

    def test_version_increments_monotonically(self):
        slot: LatestSlot[int] = LatestSlot()
        _, v0 = slot.get_with_version()
        slot.set(1)
        _, v1 = slot.get_with_version()
        slot.set(2)
        _, v2 = slot.get_with_version()
        assert v0 == 0
        assert v1 == 1
        assert v2 == 2

    def test_wait_new_returns_immediately_if_already_newer(self):
        slot: LatestSlot[int] = LatestSlot()
        slot.set(42)
        value, version = slot.wait_new(last_seen_version=0, timeout=0.5)
        assert value == 42
        assert version == 1

    def test_wait_new_times_out(self):
        slot: LatestSlot[int] = LatestSlot()
        slot.set(7)
        start = time.monotonic()
        value, version = slot.wait_new(last_seen_version=1, timeout=0.2)
        elapsed = time.monotonic() - start
        # Should block ~0.2s then return latest.
        assert 0.15 <= elapsed <= 0.5
        assert value == 7
        assert version == 1

    def test_wait_new_wakes_on_set(self):
        slot: LatestSlot[int] = LatestSlot()
        results: list[tuple[int | None, int]] = []

        def waiter():
            results.append(slot.wait_new(last_seen_version=0, timeout=2.0))

        t = threading.Thread(target=waiter)
        t.start()
        time.sleep(0.1)
        slot.set(99)
        t.join(timeout=1.0)
        assert not t.is_alive()
        assert results == [(99, 1)]

    def test_concurrent_writers(self):
        """1000 writers from 10 threads — final version must equal total writes."""
        slot: LatestSlot[int] = LatestSlot()
        n_threads = 10
        n_per_thread = 100

        def writer(start_val: int):
            for i in range(n_per_thread):
                slot.set(start_val + i)

        threads = [threading.Thread(target=writer, args=(i * 1000,)) for i in range(n_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        _, version = slot.get_with_version()
        assert version == n_threads * n_per_thread

    def test_clear(self):
        slot: LatestSlot[int] = LatestSlot()
        slot.set(5)
        slot.clear()
        assert slot.get() is None


class TestControlBus:
    def test_fifo_order(self):
        bus = ControlBus()
        for i in range(5):
            bus.put(i)
        out = [bus.get() for _ in range(5)]
        assert out == [0, 1, 2, 3, 4]

    def test_get_timeout(self):
        import queue
        bus = ControlBus()
        with pytest.raises(queue.Empty):
            bus.get(timeout=0.05)

    def test_qsize_and_empty(self):
        bus = ControlBus()
        assert bus.empty()
        bus.put("a")
        bus.put("b")
        assert bus.qsize() == 2
        assert not bus.empty()
        bus.get()
        assert bus.qsize() == 1

    def test_multi_producer_multi_consumer(self):
        bus = ControlBus(maxsize=0)
        n_items = 200
        consumed: list[int] = []
        lock = threading.Lock()

        def producer(start: int, count: int):
            for i in range(count):
                bus.put(start + i)

        def consumer(n_to_take: int):
            for _ in range(n_to_take):
                v = bus.get(timeout=2.0)
                with lock:
                    consumed.append(v)

        prods = [threading.Thread(target=producer, args=(i * 1000, 100)) for i in range(2)]
        cons = [threading.Thread(target=consumer, args=(100,)) for _ in range(2)]
        for t in prods + cons:
            t.start()
        for t in prods + cons:
            t.join(timeout=5.0)

        assert len(consumed) == n_items
        assert sorted(consumed) == sorted(list(range(100)) + list(range(1000, 1100)))
