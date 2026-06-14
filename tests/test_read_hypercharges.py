import pytest
from pathlib import Path

from revente.read_hypercharges import _parse_power, _geom_for

FIX = Path(__file__).parent / "fixtures" / "hc"


def test_parse_power_eleven():
    assert _parse_power("11") == 11


def test_parse_power_single_digit():
    assert _parse_power("1") == 1
    assert _parse_power("4") == 4


def test_parse_power_noise_returns_none():
    assert _parse_power("") is None
    assert _parse_power("x") is None
    assert _parse_power(None) is None


def test_parse_power_clamped_range():
    assert _parse_power("9999") is None


def test_geom_for_phone():
    g = _geom_for(2340, 1080)
    assert len(g["cells"]) == 6
    assert "brawlers_btn" in g and "detail_hc" in g and "scroll" in g


def test_geom_for_unknown_aspect_defaults_phone():
    assert _geom_for(1920, 1080)["cells"] == _geom_for(2340, 1080)["cells"]


def _img(name):
    Image = pytest.importorskip("PIL.Image", reason="Pillow not installed")
    return Image.open(FIX / name).convert("RGB")


def test_detail_has_hypercharge_positive():
    pytest.importorskip("cv2")
    pytest.importorskip("numpy")
    from revente.read_hypercharges import _detail_has_hypercharge
    img = _img("maisie_detail.png")
    w, h = img.size
    assert _detail_has_hypercharge(img, w, h) is True


def test_detail_has_hypercharge_negative():
    pytest.importorskip("cv2")
    pytest.importorskip("numpy")
    from revente.read_hypercharges import _detail_has_hypercharge
    img = _img("shelly_detail.png")
    w, h = img.size
    assert _detail_has_hypercharge(img, w, h) is False


def test_detail_has_hypercharge_nonmaxed_is_a_false_positive_alone():
    # A NON-maxed detail (Bull P1) shows purple power-up circles in the same
    # corner → magenta fires. This is why callers MUST also gate on _detail_is_maxed.
    pytest.importorskip("cv2")
    pytest.importorskip("numpy")
    from revente.read_hypercharges import _detail_has_hypercharge
    img = _img("bull_detail_p1.png")
    w, h = img.size
    assert _detail_has_hypercharge(img, w, h) is True  # magenta alone is fooled


def test_detail_is_maxed_positive():
    pytest.importorskip("cv2")
    pytest.importorskip("numpy")
    pytest.importorskip("easyocr")  # _detail_is_maxed runs OCR
    from revente.read_hypercharges import _detail_is_maxed
    for name in ("maisie_detail.png", "shelly_detail.png"):
        img = _img(name)
        w, h = img.size
        assert _detail_is_maxed(img, w, h) is True, name


def test_detail_is_maxed_negative():
    pytest.importorskip("cv2")
    pytest.importorskip("numpy")
    pytest.importorskip("easyocr")
    from revente.read_hypercharges import _detail_is_maxed
    img = _img("bull_detail_p1.png")  # P1, not maxed
    w, h = img.size
    assert _detail_is_maxed(img, w, h) is False


def test_maxed_gate_excludes_nonmaxed_false_positive():
    # The combined gate (maxed AND flame) is correct where the flame alone is not:
    # Bull → maxed False → excluded; Maisie → both True → counted.
    pytest.importorskip("cv2")
    pytest.importorskip("numpy")
    pytest.importorskip("easyocr")
    from revente.read_hypercharges import _detail_is_maxed, _detail_has_hypercharge
    bull = _img("bull_detail_p1.png"); bw, bh = bull.size
    maisie = _img("maisie_detail.png"); mw, mh = maisie.size
    assert (_detail_is_maxed(bull, bw, bh) and _detail_has_hypercharge(bull, bw, bh)) is False
    assert (_detail_is_maxed(maisie, mw, mh) and _detail_has_hypercharge(maisie, mw, mh)) is True
