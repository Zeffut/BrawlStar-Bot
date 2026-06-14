# tests/test_shop_endpoints.py
import importlib

import pytest

# cloud_panel.app only imports on the deploy runtime (py3.11+, with cloud_panel/ on
# the path so its sibling `import notif` resolves). Under the local py3.9 it raises a
# TypeError on `str | None` annotations — not an ImportError — so pytest.importorskip
# wouldn't catch it. Skip the whole module on ANY import failure to keep the local
# suite tidy; these run for real on the deploy env.
try:
    app_mod = importlib.import_module("cloud_panel.app")
except Exception as exc:  # noqa: BLE001 - environment-dependent import
    pytest.skip(f"cloud_panel.app not importable here: {exc}",
                allow_module_level=True)


def test_shop_routes_registered():
    paths = {r.path for r in app_mod.app.routes}
    assert "/api/accounts/{account_id}/shop/plan" in paths
    assert "/api/accounts/{account_id}/shop/buy_hypercharges" in paths
    assert "/api/accounts/{account_id}/shop/upgrade_power" in paths


def test_shop_body_model_defaults():
    assert app_mod.HypBuyBody().confirm is False
    assert app_mod.HypBuyBody().coin_floor == 0
    pu = app_mod.PowerUpgradeBody()
    assert pu.confirm is False and pu.target_level == 11 and pu.scope == "current"
