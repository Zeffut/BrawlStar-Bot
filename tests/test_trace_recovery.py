import sys
import types


def _stub_game_api_deps():
    """Stub heavy / platform-specific dependencies so game_api can be imported
    under Python 3.9 in CI without ADB, tomllib, or scrcpy."""
    for name in ("device", "state_finder", "state_finder.main", "utils"):
        if name not in sys.modules:
            sys.modules[name] = types.ModuleType(name)
    sys.modules["state_finder.main"].get_state = lambda *a, **k: None
    sys.modules["utils"].extract_text_and_positions = lambda *a, **k: {}
    sys.modules["device"].adb_serial = getattr(
        sys.modules.get("device"), "adb_serial", lambda: "stub"
    )
    # If another test file's collection stubbed game_api as a bare module
    # (e.g. test_trace_reconcile), force-remove so the real module is imported.
    mod = sys.modules.get("game_api")
    if mod is not None and not hasattr(mod, "GameAPI"):
        sys.modules.pop("game_api", None)


_stub_game_api_deps()


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
