# tests/test_shop_endpoints.py
import pytest


def test_shop_routes_registered():
    app_mod = pytest.importorskip("cloud_panel.app")
    paths = {r.path for r in app_mod.app.routes}
    assert "/api/accounts/{account_id}/shop/plan" in paths
    assert "/api/accounts/{account_id}/shop/buy_hypercharges" in paths
    assert "/api/accounts/{account_id}/shop/upgrade_power" in paths


def test_shop_body_model_defaults():
    app_mod = pytest.importorskip("cloud_panel.app")
    assert app_mod.HypBuyBody().confirm is False
    assert app_mod.HypBuyBody().coin_floor == 0
    pu = app_mod.PowerUpgradeBody()
    assert pu.confirm is False and pu.target_level == 11 and pu.scope == "current"
