"""Tests for resolve_equipped_to_canonical — the read-back reconciliation that
maps the OCR'd equipped-brawler name (English or localized French) back to the
canonical owned-brawler name, so a mis-tapped selection doesn't get the match
recorded under the wrong brawler ("Playing as carl" while jessie is equipped)."""
import pytest

# Skip cleanly if heavy bot deps aren't installed (lobby_automation pulls in
# utils→toml, easyocr, …). On the HP / CI with full requirements these succeed.
try:
    from lobby_automation import resolve_equipped_to_canonical as resolve
except ImportError as _exc:
    pytest.skip(f"lobby_automation deps missing: {_exc}", allow_module_level=True)


ROSTER = ["carl", "jessie", "colt", "8-bit", "barley", "crow", "bo"]


def test_english_exact_match():
    assert resolve("jessie", ROSTER) == "jessie"
    assert resolve("carl", ROSTER) == "carl"


def test_french_alias_maps_to_english():
    # The game shows the localized name; we must record the English one.
    assert resolve("bartaba", ROSTER) == "barley"
    assert resolve("corbac", ROSTER) == "crow"


def test_8bit_french_variants():
    # 8-bit's FR name is "Arcade" (also historically read as A.R.K.A.D/'airkad').
    assert resolve("arcade", ROSTER) == "8-bit"
    assert resolve("airkad", ROSTER) == "8-bit"


def test_ocr_trophy_badge_fused_onto_name():
    # OCR often fuses the trophy count onto the name; the full name inside the
    # token still resolves.
    assert resolve("corbac1234", ROSTER) == "crow"
    assert resolve("jessie760", ROSTER) == "jessie"


def test_the_carl_jessie_bug():
    # The exact reported case: the bot intended carl but jessie is equipped.
    # Reading "jessie" back must resolve to jessie, NOT carl.
    assert resolve("jessie", ROSTER) == "jessie"


def test_no_confident_match_returns_none():
    # An OCR string that matches nothing in the roster → None ("can't tell"),
    # so the caller keeps the intended name rather than reconciling to a wrong
    # brawler.
    assert resolve("zzqwxyz", ROSTER) is None


def test_short_token_is_too_ambiguous():
    # A 1–2 char OCR token would fuzzy-hit half the roster — bail to None.
    assert resolve("bo", ROSTER) is None
    assert resolve("x", ROSTER) is None


def test_empty_inputs():
    assert resolve("", ROSTER) is None
    assert resolve(None, ROSTER) is None
    assert resolve("jessie", []) is None
    assert resolve("jessie", None) is None


def test_accent_and_punctuation_insensitive():
    # Béa → bea, El Costo → elcosto, etc. (normalize strips accents/punctuation)
    assert resolve("Béa", ["bea", "carl"]) == "bea"
    assert resolve("D'jinn", ROSTER + ["gene"]) == "gene"
