"""One-off, explicit action: force-push ARTICLE.DESIGNATION as the
WooCommerce name for every already-synced product.

run_sync() (engine.py) deliberately never touches an existing product's
name after creation, so a name edited by hand on the storefront sticks.
That's the right default -- but it also means products whose names were
mangled by the since-removed name-cleaner (abbreviation expansion / Title
Case, which stripped reference/packaging information some articles'
names actually needed) never get fixed by a normal sync. This is the
explicit, user-triggered override: reset every already-synced product's
name back to the raw ERP designation.

Only touches products the state store already knows a WooCommerce id for
-- nothing to "fix" for articles that have never been synced, since
they'll get the correct raw name on their first creation via the normal
sync.
"""

import logging

from app.db import queries
from app.db.firebird_client import connect as connect_firebird
from app.sync.state_store import StateStore
from app.sync.woocommerce_client import WooCommerceClient, WooCommerceError

log = logging.getLogger(__name__)


def run_name_resync(cfg, dry_run=False, log_fn=None):
    """Returns a report dict:
    {"updated": [ref_art, ...], "not_tracked": int, "errors": [...],
     "payloads": [...] (dry-run only)}
    """
    emit = log_fn or (lambda msg: log.info(msg))

    con = connect_firebird(cfg)
    try:
        familles = queries.fetch_familles(con)
        articles = queries.fetch_articles(
            con, familles, filter_boutique_visible=cfg["sync"]["filter_boutique_visible"]
        )
    finally:
        con.close()
    emit(f"Read {len(articles)} article(s) from Firebird (after sync-scope filter).")

    report = {"updated": [], "not_tracked": 0, "errors": [], "payloads": []}

    with StateStore(cfg["state_db_path"]) as store:
        # targets as (ref_art, wc_product_id, name)
        targets = []
        for article in articles:
            ref = article["ref_art"]
            existing = store.get(ref)
            if not existing or not existing.get("wc_product_id"):
                report["not_tracked"] += 1
                continue
            targets.append((ref, existing["wc_product_id"], article["designation"]))

        emit(f"{len(targets)} already-synced product(s) will have their name reset "
             f"to the raw ERP designation.")

        if dry_run:
            report["payloads"] = [
                {"ref_art": ref, "id": wc_id, "name": name} for ref, wc_id, name in targets
            ]
            emit(f"[dry-run] Would update {len(targets)} product(s).")
            return report

        if not targets:
            emit("Done. Nothing to update.")
            return report

        wc_cfg = cfg["woocommerce"]
        wc_client = WooCommerceClient(
            site_url=wc_cfg["site_url"],
            consumer_key=wc_cfg["consumer_key"],
            consumer_secret=wc_cfg["consumer_secret"],
        )

        def _batch_progress(chunk_num, total_chunks, item_count):
            emit(f"Sending batch {chunk_num}/{total_chunks} ({item_count} item(s))...")

        batch = [{"id": wc_id, "name": name} for _ref, wc_id, name in targets]
        try:
            result = wc_client.batch_products(update=batch, progress_fn=_batch_progress)
            updated_ids = {row.get("id") for row in result["update"] if not row.get("error")}
            report["updated"] = [ref for ref, wc_id, _name in targets if wc_id in updated_ids]
            for ref, wc_id, _name in targets:
                if wc_id not in updated_ids:
                    report["errors"].append({"ref_art": ref, "error": "update not confirmed"})
        except WooCommerceError as exc:
            emit(f"Batch name resync failed: {exc}")
            for ref, _wc_id, _name in targets:
                report["errors"].append({"ref_art": ref, "error": str(exc)})

    emit(f"Done. updated={len(report['updated'])} not_tracked={report['not_tracked']} "
         f"errors={len(report['errors'])}")
    return report
