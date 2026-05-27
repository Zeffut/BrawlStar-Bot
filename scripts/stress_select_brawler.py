"""Stress-test the improved select_brawler.

Cycles through several brawlers and reports pass/fail per attempt.
Requires the game to be on the LOBBY screen and the bot NOT running.
"""
from __future__ import annotations

import os
import sys
import time
import warnings

os.environ.setdefault("ORT_LOGGING_LEVEL", "3")
warnings.filterwarnings("ignore")

from logging_setup import setup_logging
setup_logging()

import logging
log = logging.getLogger("test")

from window_controller import WindowController
from lobby_automation import LobbyAutomation

# Brawlers to test (rotate through owned ones).
TEST_BRAWLERS = ["colt"]


def main() -> int:
    print(">> creating WindowController…", flush=True)
    t0 = time.time()
    wc = WindowController()
    print(f">> WindowController ready in {time.time()-t0:.1f}s", flush=True)
    print(">> first screenshot…", flush=True)
    t0 = time.time()
    wc.screenshot()
    print(f">> screenshot ok in {time.time()-t0:.1f}s "
          f"(size={wc.width}x{wc.height})", flush=True)
    time.sleep(2)
    la = LobbyAutomation(wc)
    results = []
    for i, brawler in enumerate(TEST_BRAWLERS, 1):
        log.info("=== test %d/%d : %s ===", i, len(TEST_BRAWLERS), brawler)
        t0 = time.time()
        try:
            la.select_brawler(brawler)
            elapsed = time.time() - t0
            results.append((brawler, True, elapsed, ""))
            log.info("OK in %.1fs", elapsed)
        except Exception as exc:
            elapsed = time.time() - t0
            results.append((brawler, False, elapsed, str(exc)))
            log.exception("FAILED after %.1fs", elapsed)
        time.sleep(3)
    log.info("")
    log.info("=== SUMMARY ===")
    ok = sum(1 for _, success, _, _ in results if success)
    for b, success, elapsed, err in results:
        log.info("  %s %s  %.1fs  %s",
                 "✓" if success else "✗", b, elapsed, err)
    log.info("Total: %d/%d passed", ok, len(results))
    return 0 if ok == len(results) else 1


if __name__ == "__main__":
    code = main()
    # scrcpy keeps non-daemon threads alive; force-exit so the script
    # returns to the shell immediately.
    os._exit(code)
