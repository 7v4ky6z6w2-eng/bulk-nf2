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


def _make_con(article_rows, famille_rows=None, barcode_rows=None):
    return FakeConnection({
        "ARTICLE": article_rows,
        "FAMILLE": famille_rows or [("FAM1", "Stylos", 1)],
        "EQUIV_CBARRES": barcode_rows or [],
    })


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
