from revente.read_currencies import _crops_for, parse_currency_number


def test_plain_number():
    assert parse_currency_number("26157") == 26157


def test_with_thousands_separators():
    assert parse_currency_number("1 647") == 1647
    assert parse_currency_number("1,647") == 1647


def test_picks_longest_run_ignoring_noise():
    assert parse_currency_number("x 43  26157") == 26157


def test_no_digits_returns_none():
    assert parse_currency_number("abc") is None


def test_crops_for_16_9_emulator():
    # BlueStacks 16:9 → trophies/gems/gold layout.
    crops = _crops_for(2560, 1440)
    assert set(crops) == {"trophies", "gems", "gold"}


def test_crops_for_tall_phone():
    # ~19.5:9 phone (Mi9T 2340×1080) → bling/gold/gems; trophies omitted
    # (stylised-font OCR is unreliable, trophies come from brawlace).
    crops = _crops_for(2340, 1080)
    assert set(crops) == {"bling", "gold", "gems"}
    assert "trophies" not in crops
