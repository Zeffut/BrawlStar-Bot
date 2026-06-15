"""Unit tests for sale_prep — the auto sale-prep orchestration. Fully mocked
(no device, no network): ShopActionEngine and the config/state are stubbed."""
import pytest

import sale_prep


# --- fakes ---------------------------------------------------------------

class _FakeAction:
    def __init__(self, kind):
        self.kind = kind


class _FakeReport:
    def __init__(self, planned_kinds, coins_before=10000, coins_after=5000):
        self.planned = [_FakeAction(k) for k in planned_kinds]
        self.results = []
        self.coins_before = coins_before
        self.coins_after = coins_after

    def as_dict(self):
        return {"planned": [a.kind for a in self.planned],
                "coins_before": self.coins_before, "coins_after": self.coins_after}


class _FakeEngine:
    instances = []

    def __init__(self, serial, *, dry_run=True, hc_cost=5000):
        self.serial = serial
        self.dry_run = dry_run
        self.calls = {}
        _FakeEngine.instances.append(self)

    def buy_hypercharges(self, *, max_count=None, coin_floor=0, confirm=False):
        self.calls["buy_confirm"] = confirm
        return _FakeReport(["buy_hypercharge", "buy_hypercharge"], 10000, 0)

    def upgrade_power(self, *, target_level=11, scope="current", confirm=False,
                      max_steps=11, max_brawlers=1, current_power=None):
        self.calls["up_confirm"] = confirm
        self.calls["up_scope"] = scope
        return _FakeReport(["upgrade_power"], 0, 0)


@pytest.fixture
def fake_engine(monkeypatch):
    _FakeEngine.instances = []
    import revente.shop_actions as S
    monkeypatch.setattr(S, "ShopActionEngine", _FakeEngine)
    return _FakeEngine


@pytest.fixture
def tmp_state(monkeypatch, tmp_path):
    p = tmp_path / "sale_prep_state.json"
    monkeypatch.setattr(sale_prep, "_STATE_PATH", p)
    sale_prep._in_progress.clear()
    return p


def _set_mode(monkeypatch, m):
    monkeypatch.setattr(sale_prep, "mode", lambda: m)


# --- mode() --------------------------------------------------------------

def test_mode_default_and_values(monkeypatch):
    import toml
    monkeypatch.setattr("os.path.exists", lambda p: True)
    monkeypatch.setattr(toml, "load", lambda p: {})
    assert sale_prep.mode() == "plan"
    monkeypatch.setattr(toml, "load", lambda p: {"sale_prep_mode": "live"})
    assert sale_prep.mode() == "live"
    monkeypatch.setattr(toml, "load", lambda p: {"sale_prep_mode": "off"})
    assert sale_prep.mode() == "off"
    monkeypatch.setattr(toml, "load", lambda p: {"sale_prep_mode": "garbage"})
    assert sale_prep.mode() == "plan"


# --- state ---------------------------------------------------------------

def test_completed_roundtrip(tmp_state):
    assert sale_prep.completed("#T", 25000) is False
    sale_prep.mark_done("#T", 25000)
    assert sale_prep.completed("#T", 25000) is True
    assert sale_prep.completed("#T", 26000) is False  # higher target not done yet


# --- run() ---------------------------------------------------------------

def test_run_off_does_no_device_work(monkeypatch, fake_engine):
    _set_mode(monkeypatch, "off")
    s = sale_prep.run("#T", 25000, "serial")
    assert s["skipped"] is True and s["mode"] == "off"
    assert fake_engine.instances == []  # no engine constructed


def test_run_plan_is_dry_run(monkeypatch, fake_engine):
    _set_mode(monkeypatch, "plan")
    s = sale_prep.run("#T", 25000, "serial")
    assert s["live"] is False and s["mode"] == "plan"
    assert s["hc_count"] == 2 and s["upgrade_count"] == 1
    eng = fake_engine.instances[0]
    assert eng.dry_run is True
    assert eng.calls["buy_confirm"] is False and eng.calls["up_confirm"] is False
    assert eng.calls["up_scope"] == "walk"


def test_run_live_spends(monkeypatch, fake_engine):
    _set_mode(monkeypatch, "live")
    s = sale_prep.run("#T", 25000, "serial")
    assert s["live"] is True
    eng = fake_engine.instances[0]
    assert eng.dry_run is False
    assert eng.calls["buy_confirm"] is True and eng.calls["up_confirm"] is True


# --- format_summary ------------------------------------------------------

def test_format_summary_plan_and_live():
    plan = {"mode": "plan", "live": False, "hc_count": 2, "upgrade_count": 3}
    msg = sale_prep.format_summary("#ABC", 25000, plan)
    assert "#ABC" in msg and "PLAN" in msg and "2" in msg
    live = {"mode": "live", "live": True, "hc_count": 1, "upgrade_count": 0,
            "coins_before": 9000, "coins_after": 4000}
    msg2 = sale_prep.format_summary("#ABC", 25000, live)
    assert "#ABC" in msg2 and "LIVE" in msg2


# --- maybe_start idempotency --------------------------------------------

def test_maybe_start_idempotent(monkeypatch, tmp_state):
    started = []

    class _FakeThread:
        def __init__(self, *a, **k):
            pass

        def start(self):
            started.append(1)

    monkeypatch.setattr(sale_prep.threading, "Thread", _FakeThread)

    # already completed → no thread
    sale_prep.mark_done("#DONE", 25000)
    sale_prep.maybe_start(None, "#DONE", 25000, lambda m: None)
    assert started == []

    # fresh tag → starts once; the fake thread never clears _in_progress, so a
    # second call (in progress) must be a no-op.
    sale_prep.maybe_start(None, "#NEW", 25000, lambda m: None)
    assert started == [1]
    sale_prep.maybe_start(None, "#NEW", 25000, lambda m: None)
    assert started == [1]
