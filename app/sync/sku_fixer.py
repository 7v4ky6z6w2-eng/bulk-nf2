"""Find WooCommerce products with no SKU set, and let the user assign the
matching Firebird REF_ART to them.

Some deliveries never made it into NetFact2 because the WooCommerce
product on the order line had no SKU at all: order_importer.py's line
lookup needs a SKU to find the matching ARTICLE row (see
OrderImporter.import_order()'s on_missing_sku handling), and a blank SKU
can't resolve one no matter what on_missing_sku is set to. This module is
the fix: list every WC product missing a SKU, let the user type in the
REF_ART it should have been (validated against Firebird ARTICLE before
anything is written), and push the fix to WooCommerce.

NOTE on orders already (partially) imported: if a missing-SKU line was
dropped under on_missing_sku="skip_line" (the default), the order's
document was already created in Firebird WITHOUT that line -- fixing the
product's SKU here does not retroactively add the missing line to that
already-imported document, because OrderImporter.already_imported() sees
an active document for that REFDOC and treats the whole order as done.
Re-running order import after this fix picks up brand-new orders, and any
order that was skipped ENTIRELY under on_missing_sku="skip_order" -- but
an order that was partially imported under skip_line still needs its
missing line added by hand in NetFact2.
"""

import logging

from app.db.firebird_client import connect as connect_firebird
from app.sync.woocommerce_client import WooCommerceClient, WooCommerceError

log = logging.getLogger(__name__)

REF_ART_CHUNK = 500


def list_products_missing_sku(cfg, log_fn=None):
    """Every WooCommerce product (any status) whose SKU is blank. Returns
    [{"id":, "name":}, ...]."""
    emit = log_fn or (lambda msg: log.info(msg))
    wc_cfg = cfg["woocommerce"]
    client = WooCommerceClient(
        site_url=wc_cfg["site_url"], consumer_key=wc_cfg["consumer_key"],
        consumer_secret=wc_cfg["consumer_secret"],
    )
    products = client.fetch_all_products(fields=("id", "sku", "name"))
    missing = [{"id": p["id"], "name": p.get("name") or ""} for p in products if not (p.get("sku") or "").strip()]
    emit(f"{len(products)} product(s) checked, {len(missing)} missing a SKU.")
    return missing


def _validate_ref_arts(cur, ref_arts):
    """Returns the subset of 'ref_arts' that exist in Firebird ARTICLE."""
    if not ref_arts:
        return set()
    found = set()
    refs = list(ref_arts)
    for i in range(0, len(refs), REF_ART_CHUNK):
        chunk = refs[i:i + REF_ART_CHUNK]
        placeholders = ",".join("?" for _ in chunk)
        cur.execute(f"SELECT REF_ART FROM ARTICLE WHERE REF_ART IN ({placeholders})", chunk)
        found.update(str(r[0]).strip() for r in cur.fetchall())
    return found


def apply_sku_fixes(cfg, fixes, dry_run=False, log_fn=None):
    """'fixes' is [{"product_id":, "ref_art":}, ...] -- typically what the
    user typed into the GUI's editable table. Every ref_art is validated
    against Firebird ARTICLE before anything is written to WooCommerce; an
    unrecognized one is reported and skipped rather than pushed blind (a
    typo here would just create a second, differently-wrong SKU problem).

    Returns {"applied": [{"product_id":, "ref_art":}, ...],
             "invalid_ref": [{"product_id":, "ref_art":}, ...],
             "errors": [{"product_id":, "ref_art":, "error":}, ...]}"""
    emit = log_fn or (lambda msg: log.info(msg))
    fixes = [f for f in fixes if (f.get("ref_art") or "").strip()]
    report = {"applied": [], "invalid_ref": [], "errors": []}
    if not fixes:
        emit("Nothing to apply -- no REF_ART entered for any product.")
        return report

    con = connect_firebird(cfg)
    try:
        cur = con.cursor()
        found = _validate_ref_arts(cur, {f["ref_art"].strip() for f in fixes})
    finally:
        con.close()

    valid_fixes = []
    for f in fixes:
        ref = f["ref_art"].strip()
        if ref in found:
            valid_fixes.append({"product_id": f["product_id"], "ref_art": ref})
        else:
            report["invalid_ref"].append({"product_id": f["product_id"], "ref_art": ref})
            emit(f"NOT FOUND in Firebird ARTICLE: {ref!r} (product #{f['product_id']}) -- skipped.")

    if not valid_fixes:
        return report

    if dry_run:
        for f in valid_fixes:
            emit(f"[DRY] Would set product #{f['product_id']} SKU = {f['ref_art']!r}")
        report["applied"] = valid_fixes
        return report

    wc_cfg = cfg["woocommerce"]
    client = WooCommerceClient(
        site_url=wc_cfg["site_url"], consumer_key=wc_cfg["consumer_key"],
        consumer_secret=wc_cfg["consumer_secret"],
    )
    by_id = {f["product_id"]: f["ref_art"] for f in valid_fixes}
    try:
        result = client.batch_products(update=[{"id": pid, "sku": ref} for pid, ref in by_id.items()])
    except WooCommerceError as exc:
        emit(f"Batch SKU update failed: {exc}")
        for pid, ref in by_id.items():
            report["errors"].append({"product_id": pid, "ref_art": ref, "error": str(exc)})
        return report

    updated_ids = {row.get("id") for row in result["update"] if not row.get("error")}
    error_by_id = {row.get("id"): row.get("error") for row in result["update"] if row.get("error")}
    for pid, ref in by_id.items():
        if pid in updated_ids:
            report["applied"].append({"product_id": pid, "ref_art": ref})
            emit(f"Product #{pid}: SKU set to {ref!r}")
        else:
            err = error_by_id.get(pid, "update not confirmed")
            report["errors"].append({"product_id": pid, "ref_art": ref, "error": err})
            emit(f"Product #{pid}: FAILED to set SKU to {ref!r} -- {err}")

    return report
