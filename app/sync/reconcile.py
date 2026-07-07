"""One-time adoption of pre-existing WooCommerce products.

The tool identifies products it has synced via a local state store
(REF_ART -> WooCommerce product id). When a store already contains
products created by some OTHER tool, this tool doesn't know their ids --
so it can neither update them (it would try to create duplicate SKUs) nor
keep their stock in sync.

run_adopt() enumerates the existing WooCommerce catalog once, matches each
product to a Firebird article by SKU, and records REF_ART -> product id in
the state store. After that, full sync and stock sync both work off the
stored ids and never need to (re-)enumerate the store -- which also makes
them immune to storefront plugins that hide products from the REST product
listing.

NOTE: run this once with any "hide products without images" plugin
temporarily relaxed (frontend-only or disabled), otherwise the enumeration
only sees the products that plugin leaves visible.
"""

import datetime
import logging

from app.db import queries
from app.db.firebird_client import connect as connect_firebird
from app.sync.state_store import StateStore
from app.sync.woocommerce_client import WooCommerceClient

log = logging.getLogger(__name__)


def run_adopt(cfg, log_fn=None):
    """Returns {"adopted": int, "wc_total": int, "unmatched_wc": int,
    "db_total": int}."""
    emit = log_fn or (lambda msg: log.info(msg))

    con = connect_firebird(cfg)
    try:
        ref_arts = queries.fetch_all_ref_arts(con)
    finally:
        con.close()
    emit(f"Read {len(ref_arts)} article reference(s) from Firebird.")

    wc_cfg = cfg["woocommerce"]
    wc_client = WooCommerceClient(
        site_url=wc_cfg["site_url"],
        consumer_key=wc_cfg["consumer_key"],
        consumer_secret=wc_cfg["consumer_secret"],
    )
    products = wc_client.fetch_all_products(fields=("id", "sku"))
    emit(f"Read {len(products)} product(s) from WooCommerce.")
    if len(products) < len(ref_arts) / 2:
        emit("WARNING: far fewer WooCommerce products than DB articles. If a "
             "plugin hides products without images, disable it (or set it to "
             "frontend-only) and re-run adopt so all products are visible to "
             "the REST API.")

    now = datetime.datetime.now().isoformat()
    adopted = unmatched = 0
    with StateStore(cfg["state_db_path"]) as store:
        for product in products:
            sku = (product.get("sku") or "").strip()
            if not sku:
                continue
            if sku in ref_arts:
                store.adopt(sku, product["id"], now)
                adopted += 1
            else:
                unmatched += 1

    report = {"adopted": adopted, "wc_total": len(products),
              "unmatched_wc": unmatched, "db_total": len(ref_arts)}
    emit(f"Done. adopted={adopted} (WC products matched to an article), "
         f"unmatched_wc={unmatched} (WC products with no matching article).")
    return report
