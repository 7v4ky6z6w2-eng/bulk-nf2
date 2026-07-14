"""Connection diagnostics for the GUI's "Test Connection" button.

Checks Firebird and WooCommerce independently so a failure in one doesn't
hide whether the other is fine.
"""

from app.db.firebird_client import connect as connect_firebird
from app.sync.woocommerce_client import WooCommerceClient, WooCommerceError


def test_firebird(cfg):
    """Returns (ok: bool, message: str)."""
    try:
        con = connect_firebird(cfg)
        try:
            cur = con.cursor()
            cur.execute("SELECT COUNT(*) FROM ARTICLE")
            count = cur.fetchone()[0]
        finally:
            con.close()
        return True, f"Connected -- {count} article(s) in ARTICLE."
    except Exception as exc:  # noqa: BLE001 -- surfacing the raw failure is the point
        return False, str(exc)


def test_woocommerce(cfg):
    """Returns (ok: bool, message: str)."""
    wc_cfg = cfg["woocommerce"]
    if not wc_cfg["site_url"] or not wc_cfg["consumer_key"] or not wc_cfg["consumer_secret"]:
        return False, "Site URL / consumer key / consumer secret not filled in."
    try:
        client = WooCommerceClient(
            site_url=wc_cfg["site_url"],
            consumer_key=wc_cfg["consumer_key"],
            consumer_secret=wc_cfg["consumer_secret"],
        )
        client.ping()
        return True, "Connected to the WooCommerce REST API."
    except WooCommerceError as exc:
        return False, str(exc)
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)


def test_connections(cfg):
    """Returns {"firebird": {"ok": bool, "message": str},
    "woocommerce": {"ok": bool, "message": str}}."""
    fb_ok, fb_msg = test_firebird(cfg)
    wc_ok, wc_msg = test_woocommerce(cfg)
    return {
        "firebird": {"ok": fb_ok, "message": fb_msg},
        "woocommerce": {"ok": wc_ok, "message": wc_msg},
    }


def check_piece_annulee(cfg):
    """Compares the ANNULEE value on manually-created PIECE documents vs.
    the ones this tool has written (REFDOC starting with 'WC-'). Returns
    a list of (source, annulee_value, count) rows -- for the user to run
    from the Orders tab, since they only have NetFact2, not a raw SQL
    tool, to check this themselves."""
    con = connect_firebird(cfg)
    try:
        cur = con.cursor()
        cur.execute(
            "SELECT CASE WHEN REFDOC STARTING WITH 'WC-' THEN 'WC-imported' "
            "            ELSE 'other' END AS source, "
            "       ANNULEE, COUNT(*) "
            "FROM PIECE GROUP BY 1, 2 ORDER BY 1, 2"
        )
        return cur.fetchall()
    finally:
        con.close()


def lookup_pieces_by_refdoc(cfg, refdoc):
    """Returns [(nopiece, code_type_piece, datepiece, montantttc, annulee), ...]
    for every PIECE with this exact REFDOC (e.g. "WC-18226") -- lets the
    user cross-reference against what they see in the NetFact2 grid (same
    REFDOC column) to nail down, with certainty, which raw ANNULEE value
    corresponds to a document they can visually confirm is cancelled vs.
    not, rather than inferring it from aggregate counts."""
    con = connect_firebird(cfg)
    try:
        cur = con.cursor()
        cur.execute(
            "SELECT NOPIECE, CODE_TYPE_PIECE, DATEPIECE, MONTANTTTC, ANNULEE "
            "FROM PIECE WHERE REFDOC = ? ORDER BY NOPIECE",
            (refdoc,),
        )
        return cur.fetchall()
    finally:
        con.close()


def check_duplicate_wc_orders(cfg):
    """Returns [(refdoc, code_type_piece, count), ...] for WC-imported
    orders that ended up with more than one PIECE for the same order +
    document type -- evidence of documents created by the (now fixed)
    duplicate-reimport bug."""
    con = connect_firebird(cfg)
    try:
        cur = con.cursor()
        cur.execute(
            "SELECT REFDOC, CODE_TYPE_PIECE, COUNT(*) "
            "FROM PIECE WHERE REFDOC STARTING WITH 'WC-' "
            "GROUP BY REFDOC, CODE_TYPE_PIECE HAVING COUNT(*) > 1 "
            "ORDER BY REFDOC"
        )
        return cur.fetchall()
    finally:
        con.close()
