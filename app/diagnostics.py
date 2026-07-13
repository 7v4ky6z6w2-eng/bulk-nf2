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
