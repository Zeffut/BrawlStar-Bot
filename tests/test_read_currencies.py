from revente.read_currencies import parse_currency_number


def test_plain_number():
    assert parse_currency_number("26157") == 26157


def test_with_thousands_separators():
    assert parse_currency_number("1 647") == 1647
    assert parse_currency_number("1,647") == 1647


def test_picks_longest_run_ignoring_noise():
    assert parse_currency_number("x 43  26157") == 26157


def test_no_digits_returns_none():
    assert parse_currency_number("abc") is None
