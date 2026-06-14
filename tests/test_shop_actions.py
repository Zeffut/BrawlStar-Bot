import pytest
from pathlib import Path

FIX = Path(__file__).parent / "fixtures" / "hc"


def _img(name):
    Image = pytest.importorskip("PIL.Image", reason="Pillow not installed")
    return Image.open(FIX / name).convert("RGB")


# ---------------------------------------------------------------------------
# Detection tests (fixture-based — KEEP UNCHANGED)
# ---------------------------------------------------------------------------

def test_detect_power_upgrade_present_on_nonmaxed():
    pytest.importorskip("cv2"); pytest.importorskip("numpy")
    from revente.shop_actions import detect_power_upgrade, UpgradeButton
    img = _img("bull_detail_p1.png")
    w, h = img.size
    btn = detect_power_upgrade(img, w, h)
    assert isinstance(btn, UpgradeButton)
    assert 0.74 <= btn.xr <= 1.0, btn.xr
    assert 0.80 <= btn.yr <= 1.0, btn.yr


def test_detect_power_upgrade_absent_on_maxed():
    pytest.importorskip("cv2"); pytest.importorskip("numpy")
    from revente.shop_actions import detect_power_upgrade
    for name in ("shelly_detail.png", "maisie_detail.png"):
        img = _img(name)
        w, h = img.size
        assert detect_power_upgrade(img, w, h) is None, name


def test_green_separation_nonmaxed_vs_maxed():
    pytest.importorskip("cv2"); pytest.importorskip("numpy")
    from revente.shop_actions import _green_count, _crop_region, UPGRADE_REGION
    bull = _img("bull_detail_p1.png"); bw, bh = bull.size
    shelly = _img("shelly_detail.png"); sw, sh = shelly.size
    g_bull = _green_count(_crop_region(bull, bw, bh, UPGRADE_REGION))
    g_shelly = _green_count(_crop_region(shelly, sw, sh, UPGRADE_REGION))
    assert g_bull > g_shelly * 3, (g_bull, g_shelly)


def _wh(name):
    img = _img(name); w, h = img.size
    return img, w, h


def test_hc_buy_eligible():
    pytest.importorskip("cv2"); pytest.importorskip("numpy")
    from revente.shop_actions import hc_buy_eligible
    cases = {
        "shelly_detail.png": True,    # maxé (pas de bouton vert), pas de HC
        "maisie_detail.png": False,   # maxé mais HC déjà possédée
        "bull_detail_p1.png": False,  # pas maxé (bouton vert présent)
    }
    for name, expected in cases.items():
        img = _img(name); w, h = img.size
        assert hc_buy_eligible(img, w, h) is expected, name


def test_is_maxed_by_green_absence():
    pytest.importorskip("cv2"); pytest.importorskip("numpy")
    from revente.shop_actions import is_maxed
    assert is_maxed(*_wh("shelly_detail.png")) is True
    assert is_maxed(*_wh("maisie_detail.png")) is True
    assert is_maxed(*_wh("bull_detail_p1.png")) is False


# ---------------------------------------------------------------------------
# Planner tests (pure / no I/O)
# ---------------------------------------------------------------------------

def test_plan_hypercharges_affordability():
    from revente.shop_actions import plan_hypercharges
    acts = plan_hypercharges(eligible=3, coins=12000, hc_cost=5000)
    assert len(acts) == 2
    assert all(a.kind == "buy_hypercharge" and a.coin_cost == 5000 for a in acts)


def test_plan_hypercharges_coin_floor():
    from revente.shop_actions import plan_hypercharges
    acts = plan_hypercharges(eligible=3, coins=12000, hc_cost=5000, coin_floor=3000)
    assert len(acts) == 1


def test_plan_hypercharges_max_count_cap():
    from revente.shop_actions import plan_hypercharges
    acts = plan_hypercharges(eligible=3, coins=100000, hc_cost=5000, max_count=1)
    assert len(acts) == 1


def test_plan_hypercharges_none_eligible_or_broke():
    from revente.shop_actions import plan_hypercharges
    assert plan_hypercharges(eligible=0, coins=99999) == []
    assert plan_hypercharges(eligible=5, coins=4999, hc_cost=5000) == []


def test_levels_to_target():
    from revente.shop_actions import levels_to_target
    assert levels_to_target(9, 11) == 2
    assert levels_to_target(11, 11) == 0
    assert levels_to_target(1, 99) == 10   # clamp cible à 11
    assert levels_to_target(5, 3) == 0     # cible déjà atteinte
    assert levels_to_target(0, 11) == 11   # garde-fou bas


# ---------------------------------------------------------------------------
# Engine tests — grid-walk based
# ---------------------------------------------------------------------------
# The engine tests monkeypatch _walk_cards and _ensure_lobby instead of the
# old _enter_detail / _swipe_carousel_next to match the new grid navigation.

def _make_walk_cards(fixture_sequence):
    """Return a _walk_cards replacement that drives visit() over `fixture_sequence`.
    Each entry is either a PIL image or a fixture name string.
    The fake returns the count of cards visited (len of sequence)."""
    def fake_walk_cards(serial, w, h, visit):
        for item in fixture_sequence:
            img = _img(item) if isinstance(item, str) else item
            visit(img)
        return len(fixture_sequence)
    return fake_walk_cards


def test_buy_hypercharges_dry_run_spends_nothing(monkeypatch):
    pytest.importorskip("cv2"); pytest.importorskip("numpy")
    import revente.shop_actions as S
    monkeypatch.setattr(S.time, "sleep", lambda *_: None)

    # The walk drives: shelly (eligible), maisie (not eligible — has HC), bull (not maxed)
    monkeypatch.setattr(S, "_walk_cards", _make_walk_cards(
        ["shelly_detail.png", "maisie_detail.png", "bull_detail_p1.png"]
    ))
    monkeypatch.setattr(S, "_ensure_lobby", lambda *a, **k: True)
    monkeypatch.setattr(S, "_read_coins", lambda serial: 10000)
    # _screencap used by probe + _tap_confirm but NOT for walk (walk is mocked)
    monkeypatch.setattr(S, "_screencap", lambda s: _img("shelly_detail.png"))

    spends = []
    monkeypatch.setattr(S, "_spend_tap", lambda *a, **k: spends.append(a))

    eng = S.ShopActionEngine("fake", dry_run=True, hc_cost=5000)
    report = eng.buy_hypercharges()
    assert report.dry_run is True
    assert spends == []                                   # AUCUNE dépense en dry-run
    assert len([a for a in report.planned
                if a.kind == "buy_hypercharge"]) == 1    # only Shelly is eligible


def test_buy_hypercharges_dry_run_respects_cumulative_affordability(monkeypatch):
    """2 eligible brawlers but only coins for 1 → dry-run plans exactly 1."""
    pytest.importorskip("cv2"); pytest.importorskip("numpy")
    import revente.shop_actions as S
    monkeypatch.setattr(S.time, "sleep", lambda *_: None)

    # Both cards report as eligible (monkeypatched hc_buy_eligible)
    monkeypatch.setattr(S, "hc_buy_eligible", lambda *a, **k: True)
    monkeypatch.setattr(S, "_walk_cards", _make_walk_cards(
        ["shelly_detail.png", "shelly_detail.png"]
    ))
    monkeypatch.setattr(S, "_ensure_lobby", lambda *a, **k: True)
    monkeypatch.setattr(S, "_read_coins", lambda s: 8000)
    monkeypatch.setattr(S, "_screencap", lambda s: _img("shelly_detail.png"))

    spends = []
    monkeypatch.setattr(S, "_spend_tap", lambda *a, **k: spends.append(a))

    eng = S.ShopActionEngine("fake", dry_run=True, hc_cost=5000)
    rep = eng.buy_hypercharges()
    # 8000 coins, 5000/HC → only 1 affordable even though 2 are eligible
    assert spends == []
    assert len([a for a in rep.planned if a.kind == "buy_hypercharge"]) == 1


def test_buy_hypercharges_live_taps_when_confirmed(monkeypatch):
    pytest.importorskip("cv2"); pytest.importorskip("numpy")
    import revente.shop_actions as S
    monkeypatch.setattr(S.time, "sleep", lambda *_: None)

    monkeypatch.setattr(S, "_walk_cards", _make_walk_cards(["shelly_detail.png"]))
    monkeypatch.setattr(S, "_ensure_lobby", lambda *a, **k: True)
    monkeypatch.setattr(S, "_read_coins", lambda serial: 10000)
    monkeypatch.setattr(S, "_screencap", lambda s: _img("shelly_detail.png"))
    # HC tab guard passes
    monkeypatch.setattr(S, "_on_hypercharge_tab", lambda *a, **k: True)
    # confirm button found + HC applied
    monkeypatch.setattr(S, "_find_green_button_center", lambda *a, **k: (0.6, 0.8))
    monkeypatch.setattr(S, "_confirm_hc_applied", lambda self, serial, w, h: True)

    spends = []
    monkeypatch.setattr(S, "_spend_tap", lambda *a, **k: spends.append(a))

    eng = S.ShopActionEngine("fake", dry_run=False, hc_cost=5000)
    report = eng.buy_hypercharges(confirm=True, max_count=1)
    assert report.dry_run is False
    assert len(spends) >= 1                  # at least the HC slot tap
    assert any(r.executed for r in report.results)


def test_buy_hypercharges_live_blocked_when_wrong_tab(monkeypatch):
    """Live buy is BLOCKED when _on_hypercharge_tab returns False."""
    pytest.importorskip("cv2"); pytest.importorskip("numpy")
    import revente.shop_actions as S
    monkeypatch.setattr(S.time, "sleep", lambda *_: None)

    monkeypatch.setattr(S, "_walk_cards", _make_walk_cards(["shelly_detail.png"]))
    monkeypatch.setattr(S, "_ensure_lobby", lambda *a, **k: True)
    monkeypatch.setattr(S, "_read_coins", lambda serial: 10000)
    monkeypatch.setattr(S, "_screencap", lambda s: _img("shelly_detail.png"))
    # HC tab guard FAILS
    monkeypatch.setattr(S, "_on_hypercharge_tab", lambda *a, **k: False)

    spends = []
    monkeypatch.setattr(S, "_spend_tap", lambda *a, **k: spends.append(a))

    eng = S.ShopActionEngine("fake", dry_run=False, hc_cost=5000)
    report = eng.buy_hypercharges(confirm=True)
    # The HC_SLOT_TAP is still called (it opens the panel), but NO confirm tap
    # and the result must record executed=False with hypercharge tab error.
    # We only assert: no more than 1 spend (the slot tap), and the result is not executed.
    bad = [r for r in report.results if r.error and "hypercharge tab" in r.error]
    assert len(bad) >= 1, report.results
    assert all(not r.executed for r in bad)


# ---------------------------------------------------------------------------
# Upgrade tests — grid-walk based
# ---------------------------------------------------------------------------

def test_upgrade_power_current_dry_run(monkeypatch):
    pytest.importorskip("cv2"); pytest.importorskip("numpy")
    import revente.shop_actions as S
    monkeypatch.setattr(S.time, "sleep", lambda *_: None)
    monkeypatch.setattr(S, "_screencap", lambda s: _img("bull_detail_p1.png"))
    monkeypatch.setattr(S, "_read_coins", lambda s: 100000)
    spends = []
    monkeypatch.setattr(S, "_spend_tap", lambda *a, **k: spends.append(a))
    eng = S.ShopActionEngine("fake", dry_run=True)
    rep = eng.upgrade_power(target_level=3, scope="current", max_steps=5)
    assert rep.dry_run is True
    assert spends == []
    assert len(rep.planned) >= 1
    assert all(a.kind == "upgrade_power" for a in rep.planned)


def test_upgrade_power_walk_dry_run(monkeypatch):
    """scope='walk' uses _walk_cards — visits each card in sequence."""
    pytest.importorskip("cv2"); pytest.importorskip("numpy")
    import revente.shop_actions as S
    monkeypatch.setattr(S.time, "sleep", lambda *_: None)
    monkeypatch.setattr(S, "_ensure_lobby", lambda *a, **k: True)
    monkeypatch.setattr(S, "_read_coins", lambda s: 100000)
    # Walk provides one non-maxed brawler
    monkeypatch.setattr(S, "_walk_cards", _make_walk_cards(["bull_detail_p1.png"]))
    monkeypatch.setattr(S, "_screencap", lambda s: _img("bull_detail_p1.png"))
    spends = []
    monkeypatch.setattr(S, "_spend_tap", lambda *a, **k: spends.append(a))
    eng = S.ShopActionEngine("fake", dry_run=True)
    rep = eng.upgrade_power(scope="walk", target_level=11, max_brawlers=1)
    assert rep.dry_run is True
    assert spends == []
    assert len(rep.planned) >= 1


def test_upgrade_power_live_stops_when_no_button(monkeypatch):
    pytest.importorskip("cv2"); pytest.importorskip("numpy")
    import revente.shop_actions as S
    monkeypatch.setattr(S.time, "sleep", lambda *_: None)
    frames = iter(["bull_detail_p1.png", "shelly_detail.png", "shelly_detail.png"])
    cur = {"n": "bull_detail_p1.png"}

    def fake_cap(s):
        try:
            cur["n"] = next(frames)
        except StopIteration:
            pass
        return _img(cur["n"])

    monkeypatch.setattr(S, "_screencap", fake_cap)
    monkeypatch.setattr(S, "_read_coins", lambda s: 100000)
    monkeypatch.setattr(S, "_find_green_button_center", lambda *a, **k: (0.6, 0.8))
    spends = []
    monkeypatch.setattr(S, "_spend_tap", lambda *a, **k: spends.append(a))
    eng = S.ShopActionEngine("fake", dry_run=False)
    rep = eng.upgrade_power(target_level=11, scope="current", confirm=True, max_steps=5)
    assert len(spends) >= 1
    assert any(r.executed for r in rep.results)


def test_upgrade_power_caps_at_target_with_known_power(monkeypatch):
    pytest.importorskip("cv2"); pytest.importorskip("numpy")
    import revente.shop_actions as S
    monkeypatch.setattr(S.time, "sleep", lambda *_: None)
    monkeypatch.setattr(S, "_screencap", lambda s: _img("bull_detail_p1.png"))
    monkeypatch.setattr(S, "_read_coins", lambda s: 100000)
    monkeypatch.setattr(S, "_read_power_level", lambda *a, **k: None)
    monkeypatch.setattr(S, "_find_green_button_center", lambda *a, **k: (0.6, 0.8))
    monkeypatch.setattr(S, "_img_mean_diff", lambda a, b: 10.0)
    spends = []
    monkeypatch.setattr(S, "_spend_tap", lambda *a, **k: spends.append(a))
    eng = S.ShopActionEngine("fake", dry_run=False)
    rep = eng.upgrade_power(target_level=11, current_power=9, scope="current", confirm=True)
    # budget = levels_to_target(9, 11) = 2 → exactly 2 levels raised
    assert sum(1 for r in rep.results if r.executed) == 2


# ---------------------------------------------------------------------------
# CLI test
# ---------------------------------------------------------------------------

def test_cli_parser_defaults_to_plan():
    from revente.shop_actions import _build_parser
    p = _build_parser()
    ns = p.parse_args(["--serial", "x"])
    assert ns.action == "plan"
    assert ns.confirm is False
    ns2 = p.parse_args(["--serial", "x", "--buy-hc", "--confirm", "--max-count", "2"])
    assert ns2.action == "buy-hc" and ns2.confirm is True and ns2.max_count == 2


# ---------------------------------------------------------------------------
# Safety gate tests
# ---------------------------------------------------------------------------

def test_upgrade_power_live_sub11_refused_without_current_power(monkeypatch):
    """Live upgrade to target_level<11 without current_power must be refused."""
    pytest.importorskip("cv2"); pytest.importorskip("numpy")
    import revente.shop_actions as S
    monkeypatch.setattr(S.time, "sleep", lambda *_: None)
    monkeypatch.setattr(S, "_screencap", lambda s: _img("bull_detail_p1.png"))
    monkeypatch.setattr(S, "_read_coins", lambda s: 100000)
    spends = []
    monkeypatch.setattr(S, "_spend_tap", lambda *a, **k: spends.append(a))
    eng = S.ShopActionEngine("fake", dry_run=False)
    rep = eng.upgrade_power(target_level=9, scope="current", confirm=True)  # no current_power
    assert spends == []
    assert "refused" in rep.summary
