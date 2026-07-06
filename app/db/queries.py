"""Read-only queries against the ERP's Firebird database.

All functions take an open fdb cursor and return plain dicts/lists -- no
Firebird-specific types leak past this module, so the rest of the app
(hashing, WooCommerce payload building) doesn't need to know anything about
Firebird.
"""

import logging

log = logging.getLogger(__name__)

ARTICLE_COLUMNS = [
    "REF_ART", "CODEFAMILLE", "DESIGNATION",
    "PRIXACHATHT", "PRIXACHATTTC", "PRIXVENTEHT", "PRIXVENTETTC",
    "PRIXHTPROMO", "PRIXTTCPROMO", "ACTIVEPROMO", "DATEDEBPROMO", "DATEFINPROMO",
    "TAUX_TVA", "CTRLSTOCK", "PHOTO",
]

# Candidate (table, ref-column, qty-column) triples to try for stock levels,
# since the exact schema wasn't exercised by the existing import script.
# Confirm the real one with db/schema_discovery.py and trim this list.
STOCK_TABLE_CANDIDATES = [
    ("STOCK", "REF_ART", "QTE_STOCK"),
    ("STOCK", "REF_ART", "QTE"),
    ("FICHE_STOCK", "REF_ART", "QTE_STOCK"),
    ("FICHE_STOCK", "REF_ART", "QTE"),
]


def fetch_familles(con):
    """Returns {codefamille: {"intitule": str, "boutiq_visible": bool}}."""
    cur = con.cursor()
    try:
        cur.execute("SELECT CODEFAMILLE, INTITULE, BOUTIQ_VISIBLE FROM FAMILLE")
        rows = cur.fetchall()
        return {
            code: {"intitule": intitule or code, "boutiq_visible": bool(visible)}
            for code, intitule, visible in rows
        }
    except Exception:
        # BOUTIQ_VISIBLE may not exist in every install.
        cur = con.cursor()
        cur.execute("SELECT CODEFAMILLE, INTITULE FROM FAMILLE")
        return {
            code: {"intitule": intitule or code, "boutiq_visible": True}
            for code, intitule in cur.fetchall()
        }


def fetch_barcodes(con):
    """Returns {ref_art: [barcode, ...]} ordered by NOEQUIV_CBARRES (insertion
    order), from EQUIV_CBARRES -- the table actually used for barcodes in
    this install (ARTICLE.CODE_BARRES/CODE_BARRE are unused)."""
    cur = con.cursor()
    cur.execute(
        "SELECT REF_ART, CODE_BARRES FROM EQUIV_CBARRES ORDER BY REF_ART, NOEQUIV_CBARRES"
    )
    barcodes = {}
    for ref, code in cur.fetchall():
        barcodes.setdefault(ref, []).append(code)
    return barcodes


def fetch_stock_quantities(con):
    """Best-effort stock-quantity lookup. Returns {ref_art: qty}, or {} if
    none of the candidate table/column combinations exist -- run
    db/schema_discovery.py to find the real one and update
    STOCK_TABLE_CANDIDATES above."""
    for table, ref_col, qty_col in STOCK_TABLE_CANDIDATES:
        try:
            cur = con.cursor()
            cur.execute(f"SELECT {ref_col}, SUM({qty_col}) FROM {table} GROUP BY {ref_col}")
            rows = cur.fetchall()
            log.info("Using stock quantities from %s.%s", table, qty_col)
            return {ref: qty or 0 for ref, qty in rows}
        except Exception:
            continue
    log.warning("No known stock table/column combination worked; "
                "stock quantities will be omitted. Run schema_discovery.py.")
    return {}


def fetch_articles(con, familles, filter_boutique_visible=True):
    """Returns a list of article dicts, one per ARTICLE row (optionally
    restricted to families flagged BOUTIQ_VISIBLE)."""
    cur = con.cursor()
    cur.execute(f"SELECT {', '.join(ARTICLE_COLUMNS)} FROM ARTICLE")
    barcodes = fetch_barcodes(con)
    stock = fetch_stock_quantities(con)

    articles = []
    for row in cur.fetchall():
        values = dict(zip(ARTICLE_COLUMNS, row))
        codefamille = values["CODEFAMILLE"]
        famille = familles.get(codefamille, {"intitule": None, "boutiq_visible": True})

        if filter_boutique_visible and not famille["boutiq_visible"]:
            continue

        ref = values["REF_ART"]
        photo = values["PHOTO"]
        if hasattr(photo, "read"):
            photo = photo.read()

        articles.append({
            "ref_art": ref,
            "designation": values["DESIGNATION"],
            "codefamille": codefamille,
            "famille_intitule": famille["intitule"],
            "prix_achat_ht": values["PRIXACHATHT"],
            "prix_achat_ttc": values["PRIXACHATTTC"],
            "prix_vente_ht": values["PRIXVENTEHT"],
            "prix_vente_ttc": values["PRIXVENTETTC"],
            "prix_ht_promo": values["PRIXHTPROMO"],
            "prix_ttc_promo": values["PRIXTTCPROMO"],
            "active_promo": bool(values["ACTIVEPROMO"]),
            "date_deb_promo": values["DATEDEBPROMO"],
            "date_fin_promo": values["DATEFINPROMO"],
            "taux_tva": values["TAUX_TVA"],
            "ctrl_stock": bool(values["CTRLSTOCK"]),
            "photo": photo,
            "barcodes": barcodes.get(ref, []),
            "stock_qty": stock.get(ref),
        })
    return articles
