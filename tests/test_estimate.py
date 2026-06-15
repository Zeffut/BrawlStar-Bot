from revente.estimate import AccountData, estimate


def test_low_trophy_account_is_bottom_tier():
    e = estimate(AccountData(tag="#X", name="z5", trophies=2500, brawlers=25, power11=0))
    assert e.tier == "basique"
    assert e.price_max_usd <= 13
    assert e.confidence == "low"
    assert any("skins/hypercharges" in n for n in e.notes)


def test_20k_loaded_account_is_charged_tier():
    e = estimate(AccountData(tag="#Y", name="z", trophies=21000, brawlers=60,
                             power11=10, hypercharges=7, rare_skins=1))
    assert e.tier == "chargé"
    assert e.price_min_usd >= 35
    assert e.price_max_usd <= 60


def test_20k_empty_account_is_basique():
    e = estimate(AccountData(tag="#Z", name="z", trophies=21000, brawlers=40, power11=1))
    assert e.tier == "basique"
    assert e.price_min_usd >= 15 and e.price_max_usd <= 19


def test_30k_charged_top_of_grid():
    e = estimate(AccountData(tag="#W", name="z", trophies=31000, brawlers=70,
                             power11=12, hypercharges=5, rare_skins=2))
    assert e.tier == "chargé"
    assert e.price_max_usd >= 60


def test_load_override_reads_hypercharges_by_tag():
    import pytest
    pytest.importorskip("tomllib")  # estimate_account's import chain needs it (3.11+)
    from revente.estimate_account import _load_override
    # Assert PARSING behaviour, decoupled from the CSV's data values (which change
    # as accounts are prepped — the hardcoded values went stale once already).
    ov = _load_override("QPRCQ9RV2")
    assert set(ov) == {"hypercharges", "rare_skins"}
    assert isinstance(ov["hypercharges"], int) and isinstance(ov["rare_skins"], int)
    assert _load_override("#QPRCQ9RV2") == ov  # '#'-tolerant → same row
    assert _load_override("UNKNOWNTAG") == {}
    assert _load_override(None) == {}
