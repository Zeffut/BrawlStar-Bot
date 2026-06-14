import pytest
from pathlib import Path

FIX = Path(__file__).parent / "fixtures" / "hc"


def _img(name):
    Image = pytest.importorskip("PIL.Image", reason="Pillow not installed")
    return Image.open(FIX / name).convert("RGB")


def test_detect_power_upgrade_present_on_nonmaxed():
    pytest.importorskip("cv2"); pytest.importorskip("numpy")
    from revente.shop_actions import detect_power_upgrade, UpgradeButton
    img = _img("bull_detail_p1.png")
    w, h = img.size
    btn = detect_power_upgrade(img, w, h)
    assert isinstance(btn, UpgradeButton)
    # centroïde du bouton vert dans le coin bas-droite
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
    # Le compteur de vert dans la zone bouton sépare nettement P1 (gros bouton)
    # des maxés (≈0). C'est ce qui calibre GREEN_MIN_PX.
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


def test_plan_hypercharges_affordability():
    from revente.shop_actions import plan_hypercharges
    # 3 brawlers éligibles, 12000 coins, 5000/HC → 2 achats
    acts = plan_hypercharges(eligible=3, coins=12000, hc_cost=5000)
    assert len(acts) == 2
    assert all(a.kind == "buy_hypercharge" and a.coin_cost == 5000 for a in acts)


def test_plan_hypercharges_coin_floor():
    from revente.shop_actions import plan_hypercharges
    # garder >=3000 coins → on ne dépense que 9000 → 1 achat
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


def test_buy_hypercharges_dry_run_spends_nothing(monkeypatch):
    pytest.importorskip("cv2"); pytest.importorskip("numpy")
    import revente.shop_actions as S
    monkeypatch.setattr(S.time, "sleep", lambda *_: None)
    seq = ["shelly_detail.png", "maisie_detail.png", "bull_detail_p1.png",
           "shelly_detail.png"]  # le 4e répète le hash de Shelly → wrap
    it = iter(seq)
    last = {"name": seq[-1]}

    def fake_screencap(serial):
        try:
            last["name"] = next(it)
        except StopIteration:
            pass
        return _img(last["name"])

    monkeypatch.setattr(S, "_screencap", fake_screencap)
    monkeypatch.setattr(S, "_enter_detail", lambda *a, **k: True)
    monkeypatch.setattr(S, "_swipe_carousel_next", lambda *a, **k: None)
    monkeypatch.setattr(S, "_read_coins", lambda serial: 10000)
    spends = []
    monkeypatch.setattr(S, "_spend_tap", lambda *a, **k: spends.append(a))

    eng = S.ShopActionEngine("fake", dry_run=True, hc_cost=5000)
    report = eng.buy_hypercharges()
    assert report.dry_run is True
    assert spends == []                                  # AUCUNE dépense en dry-run
    assert len([a for a in report.planned
                if a.kind == "buy_hypercharge"]) == 1    # 1 seul éligible (Shelly)


def test_buy_hypercharges_live_taps_when_confirmed(monkeypatch):
    pytest.importorskip("cv2"); pytest.importorskip("numpy")
    import revente.shop_actions as S
    monkeypatch.setattr(S.time, "sleep", lambda *_: None)
    monkeypatch.setattr(S, "_enter_detail", lambda *a, **k: True)
    monkeypatch.setattr(S, "_swipe_carousel_next", lambda *a, **k: None)
    monkeypatch.setattr(S, "_read_coins", lambda serial: 10000)
    # une seule fiche éligible puis wrap immédiat
    frames = iter(["shelly_detail.png", "shelly_detail.png", "shelly_detail.png"])
    cur = {"n": "shelly_detail.png"}

    def fake_screencap(serial):
        try:
            cur["n"] = next(frames)
        except StopIteration:
            pass
        return _img(cur["n"])

    monkeypatch.setattr(S, "_screencap", fake_screencap)
    # confirm trouvé (centroïde vert factice) + vérif HC OK
    monkeypatch.setattr(S, "_find_green_button_center", lambda *a, **k: (0.6, 0.8))
    monkeypatch.setattr(S, "_confirm_hc_applied", lambda self, serial, w, h: True)
    spends = []
    monkeypatch.setattr(S, "_spend_tap", lambda *a, **k: spends.append(a))

    eng = S.ShopActionEngine("fake", dry_run=False, hc_cost=5000)
    report = eng.buy_hypercharges(confirm=True, max_count=1)
    assert report.dry_run is False
    assert len(spends) >= 1                               # au moins le tap d'achat
    assert any(r.executed for r in report.results)


def test_upgrade_power_current_dry_run(monkeypatch):
    pytest.importorskip("cv2"); pytest.importorskip("numpy")
    import revente.shop_actions as S
    monkeypatch.setattr(S.time, "sleep", lambda *_: None)
    # Bull P1 : bouton vert présent → 1 action planifiée par tick jusqu'à cible.
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


def test_upgrade_power_live_stops_when_no_button(monkeypatch):
    pytest.importorskip("cv2"); pytest.importorskip("numpy")
    import revente.shop_actions as S
    monkeypatch.setattr(S.time, "sleep", lambda *_: None)
    # 1er screen: bouton présent (Bull) ; après le tap+confirm: maxé (Shelly) → stop.
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
    monkeypatch.setattr(S, "_screencap", lambda s: _img("bull_detail_p1.png"))  # bouton toujours présent
    monkeypatch.setattr(S, "_read_coins", lambda s: 100000)
    monkeypatch.setattr(S, "_read_power_level", lambda *a, **k: None)  # OCR indispo → budget gouverne
    monkeypatch.setattr(S, "_find_green_button_center", lambda *a, **k: (0.6, 0.8))
    monkeypatch.setattr(S, "_img_mean_diff", lambda a, b: 10.0)  # chaque upgrade "vérifié"
    spends = []
    monkeypatch.setattr(S, "_spend_tap", lambda *a, **k: spends.append(a))
    eng = S.ShopActionEngine("fake", dry_run=False)
    rep = eng.upgrade_power(target_level=11, current_power=9, scope="current", confirm=True)
    # budget = levels_to_target(9, 11) = 2 → exactement 2 niveaux montés
    assert sum(1 for r in rep.results if r.executed) == 2


def test_cli_parser_defaults_to_plan():
    from revente.shop_actions import _build_parser
    p = _build_parser()
    ns = p.parse_args(["--serial", "x"])
    assert ns.action == "plan"
    assert ns.confirm is False
    ns2 = p.parse_args(["--serial", "x", "--buy-hc", "--confirm", "--max-count", "2"])
    assert ns2.action == "buy-hc" and ns2.confirm is True and ns2.max_count == 2
