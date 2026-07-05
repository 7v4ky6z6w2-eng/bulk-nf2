# -*- coding: utf-8 -*-
"""Tests unitaires de la logique de l'editeur (sans base ni interface).

Lancer :  python -m pytest test_editor_logic.py   (ou python test_editor_logic.py)
"""

import datetime

from editor_logic import (parse_number, ht_to_ttc, ttc_to_ht, apply_rounding,
                          PriceOp, TextOp, fit_text, encoded_len, detect_columns,
                          split_codes, join_codes, parse_bool, parse_date, Cols)
import label_print


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


# --- codes equivalents (liste multi-codes) --------------------------------- #
def test_split_and_join_codes():
    assert split_codes("123 ; 456;123") == ["123", "456"]
    assert split_codes("123,456\n789") == ["123", "456", "789"]
    assert split_codes("") == []
    assert split_codes(None) == []
    assert split_codes("  ") == []
    assert join_codes(["123", "456"]) == "123 ; 456"
    assert join_codes([]) == ""
    assert split_codes(join_codes(["A", "B"])) == ["A", "B"]


# --- booleens / dates ------------------------------------------------------ #
def test_parse_bool():
    assert parse_bool("oui") == 1
    assert parse_bool("Non") == 0
    assert parse_bool("1") == 1
    assert parse_bool("0") == 0
    assert parse_bool(True) == 1
    assert parse_bool(2) == 1
    assert parse_bool("") is None
    assert parse_bool(None) is None


def test_parse_date():
    assert parse_date("25/12/2026") == datetime.datetime(2026, 12, 25)
    assert parse_date("2026-12-25") == datetime.datetime(2026, 12, 25)
    assert parse_date("") is None
    assert parse_date(None) is None
    try:
        parse_date("pasunedate")
        assert False, "devrait lever ValueError"
    except ValueError:
        pass


# --- code-barres Code 128 -------------------------------------------------- #
def test_code128_widths():
    w = label_print.code128b_widths("ABC123")
    assert all(isinstance(x, int) and x > 0 for x in w)
    # commence et finit par une barre -> nombre impair de modules
    assert len(w) % 2 == 1
    # deterministe
    assert label_print.code128b_widths("ABC123") == w
    # contenu different -> encodage different
    assert label_print.code128b_widths("ABC124") != w
    # caracteres non imprimables ignores (ne plante pas)
    assert label_print.code128b_widths("\x01\x02") == label_print.code128b_widths(" ")


def test_code128_auto_is_compact_for_numbers():
    ean = "3001234500017"
    auto = label_print.code128_widths(ean)
    pure_b = label_print.code128b_widths(ean)
    # commence/finit par une barre
    assert len(auto) % 2 == 1 and auto[0] > 0
    # le jeu C rend un code numerique nettement plus court (barres plus larges)
    assert sum(auto) < sum(pure_b) * 0.75
    # deterministe ; du texte non numerique reste encode (jeu B)
    assert label_print.code128_widths(ean) == auto
    assert len(label_print.code128_widths("ABC")) % 2 == 1


# --- mise en page des etiquettes ------------------------------------------- #
def test_label_layout_barcode_model():
    m = label_print.model_by_key("M1")
    item = label_print.LabelItem(designation="Cafe 250g", barcode="123456",
                                 price=9.9)
    r = label_print.RecordingRenderer()
    label_print.layout_label(m, item, r)
    assert r.rects, "le modele M1 doit dessiner un code-barres"
    # toutes les barres tiennent dans la largeur de l'etiquette
    assert all(x + w <= m.width_mm + 0.01 for (x, y, w, h) in r.rects)
    # code-barres dans la MOITIE BASSE de l'etiquette
    assert all(y >= m.height_mm / 2 - 0.5 for (x, y, w, h) in r.rects)
    # le prix est plus GRAND que la designation
    price_t = next(t for t in r.texts if "9,90 DA" in t["s"])
    desig_t = next(t for t in r.texts if "Cafe" in t["s"])
    assert price_t["h"] > desig_t["h"]


def test_label_designation_capped():
    m = label_print.model_by_key("M1")   # max 20 caracteres
    long_name = "Article avec un nom vraiment tres tres long"
    item = label_print.LabelItem(designation=long_name, barcode="1", price=1.0)
    r = label_print.RecordingRenderer()
    label_print.layout_label(m, item, r)
    desig = next(t for t in r.texts if "Article" in t["s"])
    assert len(desig["s"]) <= m.max_designation_chars
    assert label_print._cap("abcdefgh", 5) == "abcde"
    assert label_print._cap("abc", 0) == "abc"


def test_label_layout_discount_strikes_normal_price():
    m = label_print.model_by_key("M3")
    item = label_print.LabelItem(designation="Huile", price=9.4, promo=6.9)
    r = label_print.RecordingRenderer()
    label_print.layout_label(m, item, r)
    assert not r.rects, "le modele M3 n'a pas de code-barres"
    struck = [t for t in r.texts if t["strike"]]
    assert struck and "9,40 DA" in struck[0]["s"]
    assert any("6,90 DA" in t["s"] for t in r.texts)


# --- auto-detection des colonnes ------------------------------------------ #
def test_detect_columns_real_schema():
    real = ["REF_ART", "DESIGNATION", "CODE_BARRES", "CODE_BARRE",
            "PRIXVENTEHT", "PRIXVENTETTC", "PRIXACHATHT", "TAUX_TVA", "CODEFAMILLE"]
    m = detect_columns(real)
    assert m[Cols.REF] == "REF_ART"
    assert m[Cols.PV_HT] == "PRIXVENTEHT"
    assert m[Cols.PV_TTC] == "PRIXVENTETTC"


def test_detect_columns_variant_schema():
    real = ["REFERENCE", "LIBELLE", "EAN13", "PV_HT", "PV_TTC", "TVA"]
    m = detect_columns(real)
    assert m[Cols.REF] == "REFERENCE"
    assert m[Cols.DESIGNATION] == "LIBELLE"
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
    repo.update_rows([{"ref0": "A002", "values": {Cols.PV_HT: 99.0}}])
    repo.rollback()
    a002 = next(r for r in repo.load() if r[Cols.REF] == "A002")
    assert a002[Cols.PV_HT] != 99.0


def test_demo_repo_equiv_codes():
    from article_db import DemoRepository
    repo = DemoRepository().connect()
    assert repo.has_equiv()
    eq = repo.load_equiv(["A001", "A003"])
    assert eq["A001"] == ["3001234500017", "3001234500918"]   # multi-codes
    assert "A003" not in eq
    # remplacement complet de la liste
    repo.update_equiv([("A001", ["111", "222", "333"]), ("A003", ["999"])])
    repo.commit()
    eq = repo.load_equiv(["A001", "A003"])
    assert eq["A001"] == ["111", "222", "333"]
    assert eq["A003"] == ["999"]
    # liste vide = suppression de tous les codes
    repo.update_equiv([("A003", [])])
    assert "A003" not in repo.load_equiv(["A003"])
    # recherche par code equivalent
    refs = {r[Cols.REF] for r in repo.load(search="222")}
    assert refs == {"A001"}


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


def test_demo_repo_promo_price():
    from article_db import DemoRepository
    repo = DemoRepository().connect()
    # A002 n'a pas de promo au depart
    a002 = next(r for r in repo.load() if r[Cols.REF] == "A002")
    assert not a002.get(Cols.PROMO_ACTIVE)
    repo.update_rows([{"ref0": "A002", "values": {
        Cols.PV_HT_PROMO: 2.5, Cols.PV_TTC_PROMO: 2.5, Cols.PROMO_ACTIVE: 1}}])
    repo.commit()
    a002 = next(r for r in repo.load() if r[Cols.REF] == "A002")
    assert a002[Cols.PV_TTC_PROMO] == 2.5
    assert a002[Cols.PROMO_ACTIVE] == 1
    # desactivation
    repo.update_rows([{"ref0": "A002", "values": {Cols.PROMO_ACTIVE: 0}}])
    repo.commit()
    a002 = next(r for r in repo.load() if r[Cols.REF] == "A002")
    assert a002[Cols.PROMO_ACTIVE] == 0


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
