"""Tests for utils/stats.py."""
from __future__ import annotations

import threading
import time

from bsbot.utils.stats import SessionStats


class TestSessionStats:
    def test_initial(self):
        s = SessionStats()
        snap = s.snapshot()
        assert snap["frames_seen"] == 0
        assert snap["matches_started"] == 0
        assert snap["mean_ips"] is None
        assert snap["win_rate"] is None

    def test_record_frame_counts(self):
        s = SessionStats()
        for _ in range(5):
            s.record_frame()
            time.sleep(0.01)
        assert s.frames_seen == 5
        ips = s.mean_ips()
        assert ips is not None
        # 1 / 0.01s = 100 ips theoretical; allow loose bounds for CI jitter.
        assert 10 < ips < 300

    def test_state_transitions(self):
        s = SessionStats()
        s.record_state_transition("lobby")
        s.record_state_transition("match")     # match_started 1
        s.record_state_transition("match")     # no-op (same state)
        s.record_state_transition("end")       # match_completed 1
        s.record_state_transition("lobby")
        s.record_state_transition("match")     # match_started 2
        s.record_state_transition("disconnect")  # disconnect 1

        snap = s.snapshot()
        assert snap["matches_started"] == 2
        assert snap["matches_completed"] == 1
        assert snap["disconnects"] == 1

    def test_starting_state_doesnt_count_as_match(self):
        s = SessionStats()
        s.record_state_transition("starting")
        s.record_state_transition("match")  # from starting → still counts as new match
        assert s.matches_started == 1

    def test_win_rate(self):
        s = SessionStats()
        s.victories = 3
        s.defeats = 2
        assert s.win_rate() == 0.6
        assert s.snapshot()["win_rate"] == 0.6

    def test_concurrent_record_frame_thread_safe(self):
        s = SessionStats()

        def hammer():
            for _ in range(1000):
                s.record_frame()

        threads = [threading.Thread(target=hammer) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert s.frames_seen == 4000

    def test_snapshot_is_json_safe(self):
        import json
        s = SessionStats()
        s.record_frame()
        s.record_state_transition("match")
        s.victories = 1
        s.defeats = 1
        json.dumps(s.snapshot())  # must not raise

    def test_record_error(self):
        s = SessionStats()
        s.record_error()
        s.record_error()
        assert s.snapshot()["errors"] == 2
