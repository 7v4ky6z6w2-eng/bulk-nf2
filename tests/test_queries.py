from app.db import queries


class FakeCursor:
    def __init__(self, table_rows):
        self.table_rows = table_rows
        self._rows = []

    def execute(self, sql, params=None):
        sql_upper = sql.upper()
        for table, rows in self.table_rows.items():
            if f"FROM {table}" in sql_upper:
                self._rows = rows
                return
        raise Exception(f"no such table (simulated): {sql}")

    def fetchall(self):
        return self._rows


class FakeConnection:
    def __init__(self, table_rows):
        self.table_rows = table_rows

    def cursor(self):
        return FakeCursor(self.table_rows)


def _article_row(ref, designation, prestation, codefamille="FAM1"):
    # Order must match queries.ARTICLE_COLUMNS.
    return (
        ref, codefamille, designation,
        10.0, 12.0, 20.0, 24.0,        # prix achat/vente
        None, None, 0, None, None,     # promo fields
        19, True, None,                # taux_tva, ctrl_stock, photo
        prestation,                     # ART_PRESTATION
    )


def _make_con(article_rows, famille_rows=None, barcode_rows=None, stock_rows=None,
               stock_table="STOCK"):
    tables = {
        "ARTICLE": article_rows,
        "FAMILLE": famille_rows or [("FAM1", "Stylos", 1)],
        "EQUIV_CBARRES": barcode_rows or [],
    }
    if stock_rows is not None:
        tables[stock_table] = stock_rows
    return FakeConnection(tables)


def test_fetch_articles_excludes_art_prestation():
    con = _make_con([
        _article_row("REF1", "Stylo bleu", prestation=False),
        _article_row("REF2", "Livraison a domicile", prestation=True),
    ])
    familles = queries.fetch_familles(con)
    articles = queries.fetch_articles(con, familles, filter_boutique_visible=True)
    refs = [a["ref_art"] for a in articles]
    assert refs == ["REF1"]


def test_fetch_articles_respects_boutique_visible_filter():
    con = _make_con(
        [_article_row("REF1", "Stylo bleu", prestation=False, codefamille="FAM_HIDDEN")],
        famille_rows=[("FAM_HIDDEN", "Interne", 0)],
    )
    familles = queries.fetch_familles(con)

    assert queries.fetch_articles(con, familles, filter_boutique_visible=True) == []
    assert len(queries.fetch_articles(con, familles, filter_boutique_visible=False)) == 1


def test_fetch_stock_quantities_empty_when_no_stock_table():
    # Matches the real DIFA2.FDB install: schema_discovery.py confirmed
    # neither STOCK nor FICHE_STOCK exists there.
    con = _make_con([_article_row("REF1", "Stylo bleu", prestation=False)])
    assert queries.fetch_stock_quantities(con) == {}


def test_fetch_stock_quantities_reads_first_matching_candidate_table():
    con = _make_con(
        [_article_row("REF1", "Stylo bleu", prestation=False)],
        stock_rows=[("REF1", 42), ("REF2", 7)],
        stock_table="STOCK",
    )
    assert queries.fetch_stock_quantities(con) == {"REF1": 42, "REF2": 7}


def test_fetch_stock_quantities_falls_back_to_fiche_stock():
    con = _make_con(
        [_article_row("REF1", "Stylo bleu", prestation=False)],
        stock_rows=[("REF1", 5)],
        stock_table="FICHE_STOCK",
    )
    assert queries.fetch_stock_quantities(con) == {"REF1": 5}


def test_fetch_articles_stock_qty_none_when_no_stock_table():
    con = _make_con([_article_row("REF1", "Stylo bleu", prestation=False)])
    familles = queries.fetch_familles(con)
    articles = queries.fetch_articles(con, familles, filter_boutique_visible=True)
    assert articles[0]["stock_qty"] is None


def test_fetch_articles_wires_stock_qty_when_stock_table_present():
    con = _make_con(
        [_article_row("REF1", "Stylo bleu", prestation=False)],
        stock_rows=[("REF1", 42)],
    )
    familles = queries.fetch_familles(con)
    articles = queries.fetch_articles(con, familles, filter_boutique_visible=True)
    assert articles[0]["stock_qty"] == 42
