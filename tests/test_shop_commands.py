import pytest


def _setup(monkeypatch, *, session_running=False):
    W = pytest.importorskip("worker_link")
    monkeypatch.setattr(W, "_adb_serial", lambda: "fakeserial")
    monkeypatch.setattr(W, "_resolve_local_account_id", lambda tag: 1)

    state = {"running": session_running}
    monkeypatch.setattr(W, "_cmd_session_state",
                        lambda args: {"ok": True, "state": {"active": state["running"]}})

    calls = {"buy": 0, "upgrade": 0, "dry": None}

    class FakeReport:
        def as_dict(self):
            return {"ok": True}

    class FakeEngine:
        def __init__(self, serial, dry_run=True, **kw):
            calls["dry"] = dry_run

        def buy_hypercharges(self, **kw):
            calls["buy"] += 1
            return FakeReport()

        def upgrade_power(self, **kw):
            calls["upgrade"] += 1
            return FakeReport()

    import revente.shop_actions as S
    monkeypatch.setattr(S, "ShopActionEngine", FakeEngine)
    return W, calls


def test_shop_plan_is_dry_run_and_runs(monkeypatch):
    W, calls = _setup(monkeypatch)
    out = W._cmd_shop_plan({"tag": "#T"})
    assert out["ok"] is True
    assert calls["dry"] is True and calls["buy"] == 1


def test_buy_hc_without_confirm_is_dry_run(monkeypatch):
    W, calls = _setup(monkeypatch)
    out = W._cmd_shop_buy_hypercharges({"tag": "#T"})  # pas de confirm
    assert out["ok"] is True
    assert calls["dry"] is True and calls["buy"] == 1


def test_buy_hc_confirm_refused_when_session_running(monkeypatch):
    W, calls = _setup(monkeypatch, session_running=True)
    out = W._cmd_shop_buy_hypercharges({"tag": "#T", "confirm": True})
    assert out["ok"] is False
    assert "session" in out["error"].lower()
    assert calls["buy"] == 0          # n'a pas exécuté


def test_buy_hc_confirm_runs_live_when_idle(monkeypatch):
    W, calls = _setup(monkeypatch, session_running=False)
    out = W._cmd_shop_buy_hypercharges({"tag": "#T", "confirm": True})
    assert out["ok"] is True
    assert calls["dry"] is False and calls["buy"] == 1


def test_upgrade_power_confirm_runs_live_when_idle(monkeypatch):
    W, calls = _setup(monkeypatch, session_running=False)
    out = W._cmd_shop_upgrade_power({"tag": "#T", "confirm": True})
    assert out["ok"] is True
    assert calls["dry"] is False and calls["upgrade"] == 1


def test_missing_tag_refused(monkeypatch):
    W, calls = _setup(monkeypatch)
    out = W._cmd_shop_buy_hypercharges({})  # no tag
    assert out["ok"] is False and "tag" in out["error"].lower()
