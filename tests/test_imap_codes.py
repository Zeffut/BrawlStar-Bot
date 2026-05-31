from revente.imap_codes import extract_supercell_code


def test_extracts_6_digit_code_near_keyword():
    body = "Your Supercell ID verification code is 482913. Do not share it."
    assert extract_supercell_code(body) == "482913"


def test_french_body():
    body = "Votre code de vérification Supercell ID est 100200."
    assert extract_supercell_code(body) == "100200"


def test_ignores_unrelated_long_numbers():
    body = "Order 1234567890. Your code: 654321"
    assert extract_supercell_code(body) == "654321"


def test_no_code_returns_none():
    assert extract_supercell_code("welcome to supercell") is None
