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
    "TAUX_TVA", "CTRLSTOCK", "PHOTO", "ART_PRESTATION",
]



def fetch_familles(con):
    """Returns {codefamille: {"intitule": str, "boutiq_visible": bool}}.

    BOUTIQ_VISIBLE is opt-out, not opt-in: a family where it was never set
    (NULL) is treated as visible, same as an explicit 1. Only an explicit 0
    hides it. Confirmed against the real DIFA2.FDB that some families never
    had this flag set at all -- treating NULL as "hidden" would silently
    drop every article in those families, which is not what "sync my
    catalog" means by default.
    """
    cur = con.cursor()
    try:
        cur.execute("SELECT CODEFAMILLE, INTITULE, BOUTIQ_VISIBLE FROM FAMILLE")
        rows = cur.fetchall()
        return {
            code: {"intitule": intitule or code,
                   "boutiq_visible": True if visible is None else bool(visible)}
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


def fetch_all_ref_arts(con):
    """Every REF_ART in ARTICLE (unfiltered) -- used by adoption to match
    existing WooCommerce SKUs back to real articles."""
    cur = con.cursor()
    cur.execute("SELECT REF_ART FROM ARTICLE")
    return {str(r[0]).strip() for r in cur.fetchall() if r[0] is not None}


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
    """Returns {ref_art: qty}, computed live from the ITEM ledger.

    There is no STOCK/FICHE_STOCK table in this install (confirmed via
    schema_discovery.py) -- stock is derived the same way the user's own
    already-working stock-sync tool computes it: each ITEM row's QTE is
    signed by its document type's COEFF (receptions positive, sales
    negative, etc.), so summing per article gives the current quantity on
    hand. Negative sums (data artifacts) are clamped to 0 -- not meaningful
    to show as stock.
    """
    cur = con.cursor()
    cur.execute("SELECT REF_ART, SUM(QTE * COEFF) FROM ITEM GROUP BY REF_ART")
    return {ref: max(0, int(qty or 0)) for ref, qty in cur.fetchall()}


def fetch_articles(con, familles, filter_boutique_visible=True):
    """Returns a list of article dicts, one per ARTICLE row (optionally
    restricted to families flagged BOUTIQ_VISIBLE). ART_PRESTATION rows
    (non-physical service line items) are always excluded -- they aren't
    sellable WooCommerce products."""
    cur = con.cursor()
    cur.execute(f"SELECT {', '.join(ARTICLE_COLUMNS)} FROM ARTICLE")
    barcodes = fetch_barcodes(con)
    stock = fetch_stock_quantities(con)

    articles = []
    for row in cur.fetchall():
        values = dict(zip(ARTICLE_COLUMNS, row))

        if values["ART_PRESTATION"]:
            continue

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
