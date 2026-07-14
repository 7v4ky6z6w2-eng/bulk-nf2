"""Connection diagnostics for the GUI's "Test Connection" button.

Checks Firebird and WooCommerce independently so a failure in one doesn't
hide whether the other is fine.
"""

from app.db.firebird_client import connect as connect_firebird
from app.db.schema_discovery import FIELD_TYPE_NAMES, list_columns
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


# Firebird RDB$TRIGGER_TYPE encodes timing (BEFORE/AFTER) and which
# event(s) a trigger fires on as a single int. Only the common single- and
# combined-event codes are named here; anything else is shown as a raw
# number rather than guessed at.
_TRIGGER_TYPE_NAMES = {
    1: "BEFORE INSERT", 2: "AFTER INSERT",
    3: "BEFORE UPDATE", 4: "AFTER UPDATE",
    5: "BEFORE DELETE", 6: "AFTER DELETE",
    17: "BEFORE INSERT OR UPDATE", 18: "AFTER INSERT OR UPDATE",
    25: "BEFORE INSERT OR DELETE", 26: "AFTER INSERT OR DELETE",
    27: "BEFORE UPDATE OR DELETE", 28: "AFTER UPDATE OR DELETE",
    113: "BEFORE INSERT OR UPDATE OR DELETE",
    114: "AFTER INSERT OR UPDATE OR DELETE",
}


def _trigger_type_name(code):
    return _TRIGGER_TYPE_NAMES.get(code, f"type={code}")


def list_table_triggers(cfg, table):
    """Returns [(name, type_name, inactive, source), ...] for every
    trigger on 'table'. Checks whether TIERS.SOLDE recalculation might be
    a Firebird trigger that only fires on UPDATE (not INSERT) -- which
    would exactly explain "the balance doesn't update until I open the
    document and click Modifier/Enregistrer" (that issues an UPDATE; our
    import only ever does a plain INSERT). If so, the source code (shown
    in full, since Firebird PSQL trigger bodies usually aren't obfuscated)
    reveals the real formula instead of guessing at one."""
    con = connect_firebird(cfg)
    try:
        cur = con.cursor()
        cur.execute(
            "SELECT TRIM(RDB$TRIGGER_NAME), RDB$TRIGGER_TYPE, RDB$TRIGGER_INACTIVE, "
            "       RDB$TRIGGER_SOURCE "
            "FROM RDB$TRIGGERS WHERE RDB$RELATION_NAME = ? "
            "ORDER BY RDB$TRIGGER_NAME",
            (table,),
        )
        rows = cur.fetchall()
        result = []
        for name, type_code, inactive, source in rows:
            if hasattr(source, "read"):
                source = source.read()
            if isinstance(source, bytes):
                source = source.decode("utf-8", errors="replace")
            result.append((name, _trigger_type_name(type_code), bool(inactive), source or ""))
        return result
    finally:
        con.close()


def list_table_columns(cfg, table):
    """Returns [(name, type_name, length, subtype, nullable), ...] for a
    table's columns straight from the Firebird system catalog -- needed
    to confirm exact column names (e.g. for the Remise/TVA1-3/Espece/
    Timbre/Montant Verse-style fields NetFact2's own save logic fills in
    but our INSERT currently leaves NULL) before writing SQL that
    references them, since a wrong guess breaks the INSERT outright."""
    con = connect_firebird(cfg)
    try:
        cur = con.cursor()
        rows = list_columns(cur, table)
        return [
            (name, FIELD_TYPE_NAMES.get(ftype, ftype), flen, subtype, bool(null_flag))
            for name, ftype, flen, subtype, null_flag in rows
        ]
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
