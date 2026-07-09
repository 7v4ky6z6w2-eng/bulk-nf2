"""Lightweight, standalone stock-only sync: Firebird ITEM ledger -> WooCommerce.

Ported from the user's existing wc_stock_sync.py, then made ID-driven:
instead of enumerating the WooCommerce catalog (which storefront "hide
products without images" plugins can filter out of the REST product
listing), it looks up each product's id from the local state store
(REF_ART -> id, populated by a full sync or by reconcile.run_adopt). It
tracks the last stock value pushed per product so recurring runs only send
what actually changed.

Deliberately separate from the full product sync (engine.py): stock
changes constantly (every sale) while names/prices/categories don't, so
this runs on its own tighter schedule and only ever touches
manage_stock/stock_quantity.
"""

import datetime
import logging

from app.db import queries
from app.db.firebird_client import connect as connect_firebird
from app.sync.state_store import StateStore
from app.sync.woocommerce_client import WooCommerceClient, WooCommerceError

log = logging.getLogger(__name__)


def run_stock_sync(cfg, dry_run=False, log_fn=None):
    """Returns a report dict:
    {"updated": [...], "unchanged": int, "not_tracked": int,
     "missing_in_db": int, "errors": [...], "updates_preview": [...]}
    """
    emit = log_fn or (lambda msg: log.info(msg))
    stock_cfg = cfg["stock_sync"]

    con = connect_firebird(cfg)
    try:
        db_stock = queries.fetch_stock_quantities(con)
    finally:
        con.close()
    emit(f"Read stock for {len(db_stock)} article(s) from the Firebird ITEM ledger.")

    report = {"updated": [], "unchanged": 0, "not_tracked": 0,
              "missing_in_db": 0, "errors": [], "updates_preview": []}

    with StateStore(cfg["state_db_path"]) as store:
        targets = store.get_stock_targets()  # {ref_art: (wc_product_id, last_stock)}
        emit(f"{len(targets)} product(s) tracked with a known WooCommerce id.")
        if not targets:
            emit("No products are tracked yet -- run a full product sync (--sync) "
                 "or 'Adopt existing products' (--adopt) first so the tool knows "
                 "your WooCommerce product ids.")

        # updates as (ref_art, wc_product_id, new_qty)
        updates = []
        for ref_art, qty in db_stock.items():
            target = targets.get(ref_art)
            if target is None:
                report["not_tracked"] += 1
                continue
            wc_id, last_stock = target
            if last_stock != qty:
                updates.append((ref_art, wc_id, qty))
            else:
                report["unchanged"] += 1

        if stock_cfg["zero_missing_in_db"]:
            for ref_art, (wc_id, last_stock) in targets.items():
                if ref_art not in db_stock and last_stock != 0:
                    report["missing_in_db"] += 1
                    updates.append((ref_art, wc_id, 0))

        if dry_run:
            report["updates_preview"] = [
                {"sku": ref_art, "stock_quantity": qty} for ref_art, _id, qty in updates
            ]
            emit(f"[dry-run] Would update {len(updates)} product(s).")
            return report

        if updates:
            now = datetime.datetime.now().isoformat()
            batch = [
                {"id": wc_id, "manage_stock": True, "stock_quantity": qty}
                for _ref, wc_id, qty in updates
            ]
            wc_cfg = cfg["woocommerce"]
            wc_client = WooCommerceClient(
                site_url=wc_cfg["site_url"],
                consumer_key=wc_cfg["consumer_key"],
                consumer_secret=wc_cfg["consumer_secret"],
            )
            def _batch_progress(chunk_num, total_chunks, item_count):
                emit(f"Sending batch {chunk_num}/{total_chunks} ({item_count} item(s))...")

            try:
                result = wc_client.batch_products(update=batch, chunk_size=stock_cfg["batch_size"],
                                                    progress_fn=_batch_progress)
                updated_ids = {row.get("id") for row in result["update"] if not row.get("error")}
                confirmed = [(ref, qty) for ref, wc_id, qty in updates if wc_id in updated_ids]
                store.set_last_stocks(confirmed, now)
                report["updated"] = [ref for ref, _qty in confirmed]
                for ref, wc_id, _qty in updates:
                    if wc_id not in updated_ids:
                        report["errors"].append({"sku": ref, "error": "update not confirmed"})
            except WooCommerceError as exc:
                emit(f"Batch stock update failed: {exc}")
                for ref, _wc_id, _qty in updates:
                    report["errors"].append({"sku": ref, "error": str(exc)})

    emit(f"Done. updated={len(report['updated'])} unchanged={report['unchanged']} "
         f"not_tracked={report['not_tracked']} missing_in_db={report['missing_in_db']} "
         f"errors={len(report['errors'])}")
    return report
