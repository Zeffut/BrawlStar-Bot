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
