"""Fuzzy matcher for the BRAWL BALL tile label (handles OCR typos)."""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

# Import _is_brawlball_label without triggering game_api's heavy imports.
ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("_ga_src", ROOT / "game_api.py")


def _load():
    # game_api imports easyocr-backed utils — skip the whole test if missing.
    pytest.importorskip("easyocr")
    pytest.importorskip("numpy")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod._is_brawlball_label


def test_easyocr_typos_accepted():
    fn = _load()
    for label in (
        "BRAWL BALL", "Brawl Ball", "brawlball", "brawl-ball",
        "brawibal",     # observed on Mi 9T (l→i)
        "brawhball",    # observed alt
        "brawibald",    # extra char
    ):
        assert fn(label), f"should accept {label!r}"


def test_unrelated_strings_rejected():
    fn = _load()
    for label in (
        "brawlers", "brawler menu", "showdown", "razzia de gemmes",
        "modes de jeu", "jouer", "", "ball",
    ):
        assert not fn(label), f"should NOT accept {label!r}"
