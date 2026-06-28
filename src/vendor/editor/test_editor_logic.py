# -*- coding: utf-8 -*-
"""Tests unitaires de la logique de l'editeur (sans base ni interface).

Lancer :  python -m pytest test_editor_logic.py   (ou python test_editor_logic.py)
"""

from editor_logic import (parse_number, ht_to_ttc, ttc_to_ht, apply_rounding,
                          PriceOp, TextOp, fit_text, encoded_len, detect_columns,
                          Cols)


# --- conversion de nombres ------------------------------------------------- #
def test_parse_number_french():
    assert parse_number("12,50") == 12.5
    assert parse_number("1 234,00") == 1234.0
    assert parse_number("1.234,56") == 1234.56
    assert parse_number("7.90") == 7.90
    assert parse_number("") is None
    assert parse_number("abc", 0.0) == 0.0
    assert parse_number(None) is None
    assert parse_number(3) == 3.0


# --- HT <-> TTC ------------------------------------------------------------ #
def test_ht_ttc_roundtrip():
    assert ht_to_ttc(100, 19) == 119.0
    assert ttc_to_ht(119, 19) == 100.0
    assert ht_to_ttc(None, 19) is None
    # c 0% de TVA
    assert ht_to_ttc(50, 0) == 50.0


# --- arrondis commerciaux -------------------------------------------------- #
def test_rounding_psychological():
    assert apply_rounding(12.40, "0.99") == 12.99
    assert apply_rounding(12.00, "0.99") == 12.99    # [12.00, 12.99] -> 12.99
    assert apply_rounding(12.99, "0.99") == 12.99
    assert apply_rounding(13.01, "0.99") == 13.99
    assert apply_rounding(12.995, "0.99") == 13.99   # palier suivant si on est dessous
    assert apply_rounding(12.34, "0.50") == 12.5
    assert apply_rounding(12.20, "0.50") == 12.0
    assert apply_rounding(12.34, "1") == 12.0
    assert apply_rounding(12.6, "1") == 13.0
    assert apply_rounding(12.34, None) == 12.34


# --- operations de prix ---------------------------------------------------- #
def test_price_ops():
    assert PriceOp("set", 9.99).apply(5.0) == 9.99
    assert PriceOp("inc_pct", 10).apply(100) == 110.0
    assert PriceOp("dec_pct", 20).apply(100) == 80.0
    assert PriceOp("inc_amount", 1.5).apply(10) == 11.5
    assert PriceOp("dec_amount", 2).apply(10) == 8.0
    # +10% puis arrondi .99 : 100 -> 110 -> 110.99
    assert PriceOp("inc_pct", 10, round_to="0.99").apply(100) == 110.99
    # jamais negatif
    assert PriceOp("dec_amount", 999).apply(10) == 0.0
    # current None : seul "set" produit une valeur
    assert PriceOp("inc_pct", 10).apply(None) is None
    assert PriceOp("set", 5).apply(None) == 5.0


def test_price_op_round_only():
    assert PriceOp("round", round_to="0.99").apply(12.40) == 12.99
    assert PriceOp("round", round_to="1").apply(12.40) == 12.0


# --- operations texte ------------------------------------------------------ #
def test_text_ops():
    assert TextOp("set", "X").apply("old") == "X"
    assert TextOp("clear").apply("old") is None
    assert TextOp("prefix", "PRE-").apply("123") == "PRE-123"
    assert TextOp("suffix", "-Z").apply("123") == "123-Z"
    assert TextOp("replace", value="9", find="0").apply("A0B0") == "A9B9"
    assert TextOp("copy_from").apply(None, source_value="REF1") == "REF1"
    assert TextOp("copy_from").apply("old", source_value="") is None


def test_text_replace_case_insensitive():
    op = TextOp("replace", value="X", find="ab", case_sensitive=False)
    assert op.apply("ABcabC") == "XcXC"


# --- ajustement de longueur (octets) -------------------------------------- #
def test_fit_text():
    s, trunc = fit_text("ABCDEFGH", 5)
    assert s == "ABCDE" and trunc is True
    s, trunc = fit_text("ABC", 5)
    assert s == "ABC" and trunc is False
    # accents = 1 octet en cp1252
    assert encoded_len("eee", "cp1252") == 3
    assert encoded_len("éé", "cp1252") == 2     # 'ee' accentues
    # ne coupe pas un caractere multi-octet en utf-8
    s, trunc = fit_text("ééé", 3, "utf-8")   # 2 octets chacun
    assert s == "é" and trunc is True


# --- auto-detection des colonnes ------------------------------------------ #
def test_detect_columns_real_schema():
    real = ["REF_ART", "DESIGNATION", "CODE_BARRES", "CODE_BARRE",
            "PRIXVENTEHT", "PRIXVENTETTC", "PRIXACHATHT", "TAUX_TVA", "CODEFAMILLE"]
    m = detect_columns(real)
    assert m[Cols.REF] == "REF_ART"
    assert m[Cols.PV_HT] == "PRIXVENTEHT"
    assert m[Cols.PV_TTC] == "PRIXVENTETTC"
    assert m[Cols.CODE_BARRES] == "CODE_BARRES"
    assert m[Cols.CODE_BARRE] == "CODE_BARRE"


def test_detect_columns_variant_schema():
    real = ["REFERENCE", "LIBELLE", "EAN13", "PV_HT", "PV_TTC", "TVA"]
    m = detect_columns(real)
    assert m[Cols.REF] == "REFERENCE"
    assert m[Cols.DESIGNATION] == "LIBELLE"
    assert m[Cols.CODE_BARRE] == "EAN13"
    assert m[Cols.PV_HT] == "PV_HT"
    assert m[Cols.PV_TTC] == "PV_TTC"
    assert m[Cols.TVA] == "TVA"


# --- depot de demo : prix, code-barres, reference, famille ----------------- #
def test_demo_repo_price_and_commit():
    from article_db import DemoRepository
    repo = DemoRepository().connect()
    rows = repo.load()
    a001 = next(r for r in rows if r[Cols.REF] == "A001")
    repo.update_rows([{"ref0": "A001", "values": {Cols.PV_HT: 3.00, Cols.PV_TTC: 3.57}}])
    repo.commit()
    a001 = next(r for r in repo.load() if r[Cols.REF] == "A001")
    assert a001[Cols.PV_HT] == 3.00


def test_demo_repo_rollback():
    from article_db import DemoRepository
    repo = DemoRepository().connect()
    repo.update_rows([{"ref0": "A002", "values": {Cols.CODE_BARRES: "NEWBARCODE"}}])
    repo.rollback()
    a002 = next(r for r in repo.load() if r[Cols.REF] == "A002")
    assert a002[Cols.CODE_BARRES] != "NEWBARCODE"


def test_demo_repo_create_famille_and_assign():
    from article_db import DemoRepository
    repo = DemoRepository().connect()
    assert not repo.famille_exists("SURGELE")
    repo.update_rows(
        [{"ref0": "A003", "values": {Cols.FAMILLE: "SURGELE"}}],
        new_familles=[("SURGELE", "Surgeles", 19)])
    repo.commit()
    assert repo.famille_exists("SURGELE")
    a003 = next(r for r in repo.load() if r[Cols.REF] == "A003")
    assert a003[Cols.FAMILLE] == "SURGELE"
    assert ("SURGELE", "Surgeles") in repo.list_familles()


def test_demo_repo_rename_reference():
    from article_db import DemoRepository
    repo = DemoRepository().connect()
    repo.update_rows([{"ref0": "B010", "values": {Cols.REF: "B010X"}}])
    repo.commit()
    refs = {r[Cols.REF] for r in repo.load()}
    assert "B010X" in refs and "B010" not in refs


if __name__ == "__main__":
    import sys
    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print("ok   ", name)
            except AssertionError as exc:
                failures += 1
                print("FAIL ", name, "->", exc)
    print("\n%d test(s), %d echec(s)" % (
        sum(1 for n in globals() if n.startswith("test_")), failures))
    sys.exit(1 if failures else 0)
