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
