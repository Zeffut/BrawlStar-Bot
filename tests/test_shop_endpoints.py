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
    b = app_mod.ShopBody()
    assert b.confirm is False
    assert b.target_level == 11
    assert b.scope == "current"
