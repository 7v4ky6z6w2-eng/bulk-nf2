import pytest

from app.config import DEFAULT_CONFIG
from app.sync.name_cleaner import clean_name, strip_trailing_codes

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


# -- Real examples pulled from the user's actual PRIMEOFFICE2026.FDB data --

def test_clean_name_strips_packaging_multiples():
    # "en" stays lowercase -- it's a French connector word, not the first word.
    assert clean_name("STYLO CORRECTEUR TETE EN METAL 7 ML BLACK 8448*576*24*", {}) == \
        "Stylo Correcteur Tete en Metal 7 Ml Black"


def test_clean_name_strips_glued_code_with_multiples():
    assert clean_name("Marqueur Tableau Blanc BLEU TECH4990*576*144*12*", {}) == \
        "Marqueur Tableau Blanc Bleu"


def test_clean_name_strips_dash_slash_ref_code():
    assert clean_name("STYLO CLARO TECHNIK ROUGE SR CL-2761/CRT1440", {}) == \
        "Stylo Claro Technik Rouge Sr"


def test_clean_name_strips_dash_code():
    assert clean_name("CORRECTEUR STYLO MAZE V-1203", {}) == "Correcteur Stylo Maze"


def test_clean_name_strips_colon_ref_code():
    # "D'ENCRE" stays uppercase (the apostrophe means isalpha() is False, so
    # it's not re-cased -- a known, pre-existing limitation of the ported
    # algorithm, unrelated to code-stripping); "pour" lowercases as a
    # connector word.
    assert clean_name("CARTOUCHE D'ENCRE THINKY POUR MARQUEUR NOIR REF:4995", {}) == \
        "Cartouche D'ENCRE Thinky pour Marqueur Noir"


def test_clean_name_preserves_meaningful_trailing_quantity():
    # "BOITE DE 20" (box of 20) is real, meaningful data -- a bare number
    # with no letters/separators must NOT be treated as a code. "de"
    # lowercases as a connector word.
    assert clean_name("STYLO BIC CRISTAL UP ROUGE BOITE DE 20", {}) == \
        "Stylo Bic Cristal Up Rouge Boite de 20"


def test_clean_name_preserves_format_and_weight_codes():
    # "A4", "30F" (format) and "120G" (grammage) are real specs, not codes
    # to strip -- only the trailing period gets split off as its own token.
    assert clean_name("PAPIER NOIR 30F A4 120G", {}) == "Papier Noir 30F A4 120G"


def test_strip_trailing_codes_never_empties_the_name():
    assert strip_trailing_codes("8448*576*24*") == "8448*576*24*"
