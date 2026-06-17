import sys
import types


def _stub_stage_manager_deps():
    """Stub heavy / platform-specific dependencies so stage_manager can be imported
    under Python 3.9 without ADB, tomllib, cv2, scrcpy, or config files."""
    stubs = {
        "tomllib": types.ModuleType("tomllib"),
        "device": types.ModuleType("device"),
        "state_finder": types.ModuleType("state_finder"),
        "state_finder.main": types.ModuleType("state_finder.main"),
        "utils": types.ModuleType("utils"),
        "trophy_observer": types.ModuleType("trophy_observer"),
        "cv2": types.ModuleType("cv2"),
        "numpy": types.ModuleType("numpy"),
        "requests": types.ModuleType("requests"),
        "asyncio": types.ModuleType("asyncio"),
        "lobby_automation": types.ModuleType("lobby_automation"),
        "game_api": types.ModuleType("game_api"),
        "debug_trace": types.ModuleType("debug_trace"),
    }
    for name, mod in stubs.items():
        if name not in sys.modules:
            sys.modules[name] = mod

    # Provide symbols referenced at stage_manager import time
    sys.modules["state_finder.main"].get_state = lambda *a, **k: None
    sys.modules["utils"].find_template_center = lambda *a, **k: None
    sys.modules["utils"].extract_text_and_positions = lambda *a, **k: {}
    sys.modules["utils"].load_toml_as_dict = lambda *a, **k: {
        "discord_id": 0, "personal_webhook": ""}
    sys.modules["utils"].async_notify_user = lambda *a, **k: None
    sys.modules["utils"].save_brawler_data = lambda *a, **k: None
    sys.modules["utils"].resource_path = lambda p: p
    sys.modules["trophy_observer"].TrophyObserver = object
    # Pre-populate stub attributes so monkeypatch.setattr can patch them.
    sys.modules["game_api"].get = lambda: None
    sys.modules["lobby_automation"].resolve_equipped_to_canonical = lambda ocr, cands: None
    sys.modules["debug_trace"].trace = lambda *a, **k: None


_stub_stage_manager_deps()


def test_reconcile_corrects_and_traces(monkeypatch):
    import stage_manager
    import game_api
    import lobby_automation
    import debug_trace
    sm = stage_manager.StageManager.__new__(stage_manager.StageManager)
    sm.brawlers_pick_data = [{"brawler": "rico"}]
    sm._owned_brawler_names = ["bea", "rico"]

    class FakeAPI:
        def read_current_brawler(self):
            return "bea"

    monkeypatch.setattr(game_api, "get", lambda: FakeAPI())
    monkeypatch.setattr(lobby_automation, "resolve_equipped_to_canonical",
                        lambda ocr, cands: "bea")
    calls = []
    monkeypatch.setattr(debug_trace, "trace", lambda *a, **k: calls.append((a, k)))

    final = sm._reconcile_equipped_brawler()
    assert final == "bea"
    assert sm.brawlers_pick_data[0]["brawler"] == "bea"
    assert calls[0][0][0] == "brawler_reconcile"
    d = calls[0][1]["data"]
    assert d["intended"] == "rico" and d["corrected"] is True and d["final"] == "bea"
    assert calls[0][1]["force_capture"] is True


def test_reconcile_keeps_intended_when_unreadable(monkeypatch):
    import stage_manager, game_api, debug_trace
    sm = stage_manager.StageManager.__new__(stage_manager.StageManager)
    sm.brawlers_pick_data = [{"brawler": "rico"}]
    sm._owned_brawler_names = ["bea", "rico"]

    class FakeAPI:
        def read_current_brawler(self):
            return None

    monkeypatch.setattr(game_api, "get", lambda: FakeAPI())
    monkeypatch.setattr(debug_trace, "trace", lambda *a, **k: None)
    assert sm._reconcile_equipped_brawler() == "rico"
    assert sm.brawlers_pick_data[0]["brawler"] == "rico"
