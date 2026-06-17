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
    sys.modules["game_api"].get = lambda: None
    sys.modules["lobby_automation"].resolve_equipped_to_canonical = lambda ocr, cands: None


_stub_stage_manager_deps()


def test_trace_match_result(monkeypatch):
    import stage_manager
    import debug_trace
    sm = stage_manager.StageManager.__new__(stage_manager.StageManager)
    sm.brawlers_pick_data = [{"brawler": "bea"}]
    calls = []
    monkeypatch.setattr(debug_trace, "trace", lambda *a, **k: calls.append((a, k)))
    sm._trace_match_result("victory")
    assert calls and calls[0][0][0] == "match_end"
    assert calls[0][1]["data"]["brawler"] == "bea"
    assert calls[0][1]["data"]["result"] == "victory"
    assert calls[0][1]["force_capture"] is True
