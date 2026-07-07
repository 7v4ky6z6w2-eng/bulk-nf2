"""Lightweight, standalone stock-only sync: Firebird ITEM ledger -> WooCommerce.

Ported from the user's existing wc_stock_sync.py. Deliberately separate
from the full product sync (engine.py): stock changes constantly (every
sale) while names/prices/categories don't, so this runs on its own,
tighter schedule and only ever touches manage_stock/stock_quantity --
never name, price, category, or images.
"""

import logging

from app.db import queries
from app.db.firebird_client import connect as connect_firebird
from app.sync.woocommerce_client import WooCommerceClient, WooCommerceError

log = logging.getLogger(__name__)


def run_stock_sync(cfg, dry_run=False, log_fn=None):
    """Returns a report dict:
    {"updated": [...], "unchanged": int, "no_sku": int, "missing_in_db": int,
     "errors": [...], "updates_preview": [...] (dry-run only)}
    """
    emit = log_fn or (lambda msg: log.info(msg))
    stock_cfg = cfg["stock_sync"]

    con = connect_firebird(cfg)
    try:
        db_stock = queries.fetch_stock_quantities(con)
    finally:
        con.close()
    emit(f"Read stock for {len(db_stock)} article(s) from the Firebird ITEM ledger.")

    wc_cfg = cfg["woocommerce"]
    wc_client = WooCommerceClient(
        site_url=wc_cfg["site_url"],
        consumer_key=wc_cfg["consumer_key"],
        consumer_secret=wc_cfg["consumer_secret"],
    )
    wc_products = wc_client.fetch_all_products(
        fields=("id", "sku", "stock_quantity", "manage_stock")
    )
    emit(f"Read {len(wc_products)} product(s) from WooCommerce.")

    report = {"updated": [], "unchanged": 0, "no_sku": 0, "missing_in_db": 0,
              "errors": [], "updates_preview": []}

    updates = []
    for product in wc_products:
        sku = (product.get("sku") or "").strip()
        if not sku:
            report["no_sku"] += 1
            continue
        pid = product["id"]
        wc_qty = product.get("stock_quantity")
        wc_managed = product.get("manage_stock", False)

        if sku in db_stock:
            new_qty = db_stock[sku]
            if (not wc_managed) or wc_qty != new_qty:
                updates.append({"id": pid, "sku": sku, "manage_stock": True,
                                 "stock_quantity": new_qty})
            else:
                report["unchanged"] += 1
        else:
            report["missing_in_db"] += 1
            if stock_cfg["zero_missing_in_db"] and (wc_qty != 0 or not wc_managed):
                updates.append({"id": pid, "sku": sku, "manage_stock": True,
                                 "stock_quantity": 0})

    if dry_run:
        report["updates_preview"] = updates
        emit(f"[dry-run] Would update {len(updates)} product(s).")
        return report

    if updates:
        batch_payload = [
            {"id": u["id"], "manage_stock": u["manage_stock"], "stock_quantity": u["stock_quantity"]}
            for u in updates
        ]
        try:
            result = wc_client.batch_products(update=batch_payload,
                                               chunk_size=stock_cfg["batch_size"])
            updated_ids = {row.get("id") for row in result["update"] if not row.get("error")}
            for u in updates:
                if u["id"] in updated_ids:
                    report["updated"].append(u["sku"])
                else:
                    report["errors"].append({"sku": u["sku"], "error": "update not confirmed"})
        except WooCommerceError as exc:
            emit(f"Batch stock update failed: {exc}")
            for u in updates:
                report["errors"].append({"sku": u["sku"], "error": str(exc)})

    emit(f"Done. updated={len(report['updated'])} unchanged={report['unchanged']} "
         f"no_sku={report['no_sku']} missing_in_db={report['missing_in_db']} "
         f"errors={len(report['errors'])}")
    return report
