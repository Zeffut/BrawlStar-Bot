import sale_report


def _data(**kw):
    base = dict(total=22000, brawler_count=34, p11=["maisie", "brock", "shelly"],
                below_ceiling=2, headroom=300, gold=12000, gems=1600,
                name="Zeffut5.0", tag="QPRCQ9RV2")
    base.update(kw)
    return base


def test_estimate_price_floor_scales_with_trophies():
    low, high = sale_report.estimate_price(_data(total=20000, p11=[]))
    assert 10 <= low <= 18
    assert high >= low


def test_estimate_price_p11_bonus_raises_high():
    low_a, high_a = sale_report.estimate_price(_data(total=22000, p11=[]))
    low_b, high_b = sale_report.estimate_price(_data(total=22000,
                                                     p11=["a", "b", "c", "d", "e"]))
    assert high_b > high_a


def test_build_actions_quantifies_hypercharges_when_gold_known():
    acts = sale_report.build_actions(_data(gold=12000,
                                           p11=["maisie", "brock", "shelly"]))
    joined = " ".join(acts).lower()
    assert "2" in joined
    assert "hypercharge" in joined
    assert any("gemme" in a.lower() for a in acts)


def test_build_actions_degrades_when_gold_unknown():
    acts = sale_report.build_actions(_data(gold=None))
    joined = " ".join(acts).lower()
    assert "5000" in joined
    assert "hypercharge" in joined


def test_format_telegram_has_no_raw_none_and_includes_total():
    d = _data(gold=None, gems=None)
    msg = sale_report.format_telegram(d, sale_report.build_actions(d),
                                      sale_report.estimate_price(d))
    assert "None" not in msg
    assert "22000" in msg or "22 000" in msg
    assert d["name"] in msg


def test_idempotency_round_trip(tmp_path, monkeypatch):
    monkeypatch.setattr(sale_report, "_STATE_PATH",
                        tmp_path / "sale_report_state.json")
    sale_report.mark_notified("X", 100)
    assert sale_report.already_notified("X", 100) is True
    assert sale_report.already_notified("X", 200) is False
