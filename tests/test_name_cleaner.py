import pytest

from app.config import DEFAULT_CONFIG
from app.sync.name_cleaner import clean_name

REPLACEMENTS = DEFAULT_CONFIG["sync"]["name_replacements"]


@pytest.mark.parametrize("raw,expected", [
    ("stylo bille bleu", "Stylo Bille Bleu"),
    ("STYLO BILLE BLEU", "Stylo Bille Bleu"),
    ("Stylo Bille Bleu", "Stylo Bille Bleu"),
    ("cahier   96   pages", "Cahier 96 Pages"),
    ("", ""),
    (None, ""),
])
def test_clean_name_basic_title_case(raw, expected):
    assert clean_name(raw, {}) == expected


def test_clean_name_expands_abbreviations():
    assert clean_name("STYL BIL BLU", REPLACEMENTS) == "Stylo Bille Bleu"
    assert clean_name("CAH GM", REPLACEMENTS) == "Cahier Grand Modèle"


def test_clean_name_is_case_insensitive_for_abbreviations():
    assert clean_name("styl bil blu", REPLACEMENTS) == "Stylo Bille Bleu"


def test_clean_name_lowercases_french_connectors_except_first_word():
    assert clean_name("boite de rangement", {}) == "Boite de Rangement"
    assert clean_name("de la colle", {}) == "De la Colle"


def test_clean_name_leaves_non_alpha_tokens_uppercased():
    # Everything is uppercased before re-casing; only pure-alpha tokens get
    # Title-Cased back down -- a token with a digit (like "A4") stays as
    # its uppercased form rather than reverting to the original casing.
    assert clean_name("cahier a4", {}) == "Cahier A4"
    assert clean_name("classeur b/2", {}) == "Classeur B / 2"


def test_clean_name_no_replacements_dict():
    # replacements=None must not raise.
    assert clean_name("stylo bleu", None) == "Stylo Bleu"
