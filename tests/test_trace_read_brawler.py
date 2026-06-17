import sys
import types
from PIL import Image


def _stub_game_api_deps():
    """Stub heavy / platform-specific dependencies so game_api can be imported
    under Python 3.9 in CI without ADB, tomllib, or scrcpy."""
    for name in ("device", "state_finder", "state_finder.main", "utils"):
        if name not in sys.modules:
            sys.modules[name] = types.ModuleType(name)
    # Provide the symbols game_api references at import time.
    sys.modules["state_finder.main"].get_state = lambda *a, **k: None
    sys.modules["utils"].extract_text_and_positions = lambda *a, **k: {}


_stub_game_api_deps()


def test_read_current_brawler_emits_trace(monkeypatch):
    import importlib
    # Reload game_api if previously cached without stubs.
    import game_api
    import debug_trace

    api = game_api.GameAPI(None, None)
    monkeypatch.setattr(api, "_grab", lambda: Image.new("RGB", (1280, 576)))
    monkeypatch.setattr(game_api, "extract_text_and_positions",
                        lambda crop: {"rico": (0, 0)})
    calls = []
    monkeypatch.setattr(debug_trace, "trace", lambda *a, **k: calls.append((a, k)))
    out = api.read_current_brawler()
    assert out == "rico"
    assert calls and calls[0][0][0] == "brawler_read"
    assert calls[0][1]["data"]["token"] == "rico"
    assert "rico" in calls[0][1]["data"]["ocr_raw"]
    assert calls[0][1]["crop"] is not None
