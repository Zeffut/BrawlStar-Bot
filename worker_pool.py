"""Manages multiple bot workers (one per account) for parallel operation.

Today there's a single worker (default device emulator-5554). The pool
abstraction means the panel can later spawn additional workers each
bound to a different ADB device serial without rewriting controls.

The actual `BotRunner` lives in telegram_main.py — we wrap it so a
single instance / future multiple instances expose the same API.
"""
from __future__ import annotations

import threading
from typing import Optional


class WorkerPool:
    def __init__(self) -> None:
        self._workers: dict[int, "BotWorker"] = {}
        self._lock = threading.Lock()

    def register(self, account_id: int, worker: "BotWorker") -> None:
        with self._lock:
            self._workers[account_id] = worker

    def get(self, account_id: int) -> Optional["BotWorker"]:
        with self._lock:
            return self._workers.get(account_id)

    def all(self) -> list["BotWorker"]:
        with self._lock:
            return list(self._workers.values())


class BotWorker:
    """Adapter around BotRunner that exposes account-aware controls.

    Wraps a single BotRunner and stores the bound account_id + last
    metadata, so the panel can list and control multiple workers
    uniformly.
    """

    def __init__(self, account_id: int, device_serial: str,
                 runner) -> None:
        self.account_id = account_id
        self.device_serial = device_serial
        self.runner = runner  # BotRunner instance

    def is_running(self) -> bool:
        return self.runner.is_running()

    def status(self) -> dict:
        m = self.runner.main_instance
        d: dict = {
            "account_id": self.account_id,
            "device_serial": self.device_serial,
            "running": self.is_running(),
        }
        if m is not None:
            try:
                d["state"] = getattr(m, "state", None)
                obs = m.Stage_manager.Trophy_observer
                d["current_trophies"] = obs.current_trophies
                d["current_brawler"] = m.Stage_manager.brawlers_pick_data[0]['brawler']
            except Exception:
                pass
        d["matches_in_session"] = self.runner._match_count
        d["wins"] = self.runner._win_count
        d["losses"] = self.runner._loss_count
        d["draws"] = self.runner._draw_count
        d["initial_trophies"] = self.runner._initial_trophies
        return d

    def start(self, brawler: str, target: int) -> tuple[bool, str]:
        return self.runner.start(brawler, target, 0)

    def stop(self) -> tuple[bool, str]:
        return self.runner.stop()

    def force_stop(self) -> tuple[bool, str]:
        return self.runner.force_stop()


# Module-level singleton accessed by both Telegram & panel.
POOL = WorkerPool()
