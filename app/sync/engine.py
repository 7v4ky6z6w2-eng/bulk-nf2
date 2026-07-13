"""Core sync orchestration: Firebird ARTICLE rows -> WooCommerce products.

Used identically from the CLI (--sync) and from the GUI's "Sync now"
button, so both paths behave the same way.
"""

import datetime
import hashlib
import json
import logging

from app.db import queries
from app.db.firebird_client import connect as connect_firebird
from app.images.blob_extractor import extract_image
from app.sync.name_cleaner import clean_name
from app.sync.state_store import StateStore
from app.sync.woocommerce_client import WooCommerceClient, WooCommerceError

log = logging.getLogger(__name__)


def _price_str(value):
    if value is None:
        return None
    return f"{float(value):.2f}"


def _iso(dt):
    if not dt:
        return None
    if isinstance(dt, (datetime.date, datetime.datetime)):
        return dt.isoformat()
    return str(dt)


def build_core_fields(article, cfg):
    """Fields derivable without any network call -- used both for the
    content hash and as the base of the real WooCommerce payload."""
    sync_cfg = cfg["sync"]
    price_field = "prix_vente_ttc" if sync_cfg["price_field"] == "PRIXVENTETTC" else "prix_vente_ht"
    promo_field = "prix_ttc_promo" if sync_cfg["promo_price_field"] == "PRIXTTCPROMO" else "prix_ht_promo"

    name = clean_name(article["designation"], sync_cfg.get("name_replacements"))

    fields = {
        "sku": article["ref_art"],
        "name": name,
        "regular_price": _price_str(article[price_field]),
    }

    if article["active_promo"] and article[promo_field]:
        fields["sale_price"] = _price_str(article[promo_field])
        if article["date_deb_promo"]:
            fields["date_on_sale_from"] = _iso(article["date_deb_promo"])
        if article["date_fin_promo"]:
            fields["date_on_sale_to"] = _iso(article["date_fin_promo"])

    if article["stock_qty"] is not None:
        fields["manage_stock"] = True
        fields["stock_quantity"] = int(article["stock_qty"])
    else:
        fields["manage_stock"] = False

    barcodes = article["barcodes"]
    meta_data = []
    if article["designation"] and article["designation"] != name:
        # The cleaned name can drop trailing reference/packaging codes
        # (see name_cleaner.strip_trailing_codes) -- keep the untouched
        # ERP text around too, so nothing is lost even if it's not shown.
        meta_data.append({"key": "_erp_designation", "value": article["designation"]})
    if barcodes:
        fields["global_unique_id"] = barcodes[0]
        meta_data.append({"key": "_barcode", "value": barcodes[0]})
        if len(barcodes) > 1:
            meta_data.append({"key": "_alt_barcodes", "value": ",".join(barcodes[1:])})
    if meta_data:
        fields["meta_data"] = meta_data

    return fields


def apply_auto_sale_price(core, last_regular_price):
    """If the article's regular_price (freshly computed from Firebird) is
    lower than the price WooCommerce already shows (the anchor price this
    tool last pushed as regular_price), keep regular_price at that anchor
    and push the lower Firebird price as sale_price instead -- so a price
    cut in NetFact2 shows up as a strikethrough sale on the storefront
    rather than silently replacing the base price. If the price is back
    at or above the anchor, that price becomes the new anchor and any
    previous auto-sale is cleared.

    Skips articles that already carry an explicit ACTIVEPROMO sale_price
    (build_core_fields already set one) -- that's a deliberate promo from
    NetFact2 and takes priority over this heuristic.

    Returns (possibly-modified core_fields, new_anchor_price)."""
    new_price = core.get("regular_price")
    if new_price is None:
        return core, last_regular_price
    if "sale_price" in core:
        return core, float(new_price)

    new_price_f = float(new_price)
    core = dict(core)
    if last_regular_price is not None and new_price_f < last_regular_price:
        core["regular_price"] = _price_str(last_regular_price)
        core["sale_price"] = new_price
        return core, last_regular_price

    core["sale_price"] = ""  # explicitly clear any stale auto-sale
    return core, new_price_f


def content_hash(core_fields, has_image, image_bytes=None):
    payload = dict(core_fields)
    payload["_has_image"] = has_image
    if image_bytes:
        payload["_image_hash"] = hashlib.sha256(image_bytes).hexdigest()
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()


def run_sync(cfg, dry_run=False, log_fn=None):
    """Runs one sync pass. Returns a report dict:
    {"created": [...], "updated": [...], "unchanged": int, "orphans": [...],
     "errors": [{"ref_art": ..., "error": ...}], "payloads": [...] (dry-run only)}
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

    wc_client = None
    if not dry_run:
        wc_cfg = cfg["woocommerce"]
        wp_cfg = cfg["wordpress"]
        wc_client = WooCommerceClient(
            site_url=wc_cfg["site_url"],
            consumer_key=wc_cfg["consumer_key"],
            consumer_secret=wc_cfg["consumer_secret"],
            wp_username=wp_cfg.get("username") or None,
            wp_app_password=wp_cfg.get("app_password") or None,
        )

    report = {"created": [], "updated": [], "unchanged": 0, "orphans": [],
              "errors": [], "payloads": []}

    with StateStore(cfg["state_db_path"]) as store:
        seen_refs = set()
        to_create, to_update = [], []
        # ref_art -> (article, image_source) for post-batch state updates
        pending = {}

        for i, article in enumerate(articles, 1):
            # Large catalogs can take minutes overall (category lookups,
            # image uploads); without this a long silent gap can look like
            # the sync hung even though it's working.
            if i == 1 or i % 250 == 0 or i == len(articles):
                emit(f"Processing article {i}/{len(articles)}...")

            ref = article["ref_art"]
            seen_refs.add(ref)
            existing = store.get(ref)
            core = build_core_fields(article, cfg)

            last_price = existing["last_regular_price"] if existing else None
            if cfg["sync"].get("auto_sale_on_price_drop", True):
                core, new_anchor = apply_auto_sale_price(core, last_price)
            else:
                price = core.get("regular_price")
                new_anchor = float(price) if price is not None else last_price

            image = None
            if cfg["sync"]["sync_images"]:
                image = extract_image(article["photo"], ref)

            new_hash = content_hash(core, has_image=bool(image),
                                     image_bytes=image.data if image else None)

            if existing and existing["content_hash"] == new_hash:
                report["unchanged"] += 1
                continue

            # Categories are deliberately not touched here -- the store runs
            # its own WordPress auto-categorizer plugin instead.
            payload = {k: v for k, v in core.items() if not k.startswith("_")}

            if dry_run:
                payload["images"] = ["<would upload ARTICLE.PHOTO>"] if image else []
                report["payloads"].append({"ref_art": ref, "payload": payload,
                                            "action": "update" if existing else "create"})
                continue

            image_source = "none"
            if image:
                try:
                    media_id = wc_client.upload_media(image.data, image.filename, image.mime_type)
                    payload["images"] = [{"id": media_id}]
                    image_source = "blob"
                except WooCommerceError as exc:
                    report["errors"].append({"ref_art": ref, "error": f"image: {exc}"})

            pending[ref] = (new_hash, image_source, new_anchor)
            if existing and existing["wc_product_id"]:
                payload["id"] = existing["wc_product_id"]
                to_update.append(payload)
            else:
                to_create.append(payload)

        if not dry_run and (to_create or to_update):
            def _batch_progress(chunk_num, total_chunks, item_count):
                emit(f"Sending batch {chunk_num}/{total_chunks} ({item_count} item(s))...")

            try:
                results = wc_client.batch_products(create=to_create, update=to_update,
                                                     progress_fn=_batch_progress)
            except WooCommerceError as exc:
                emit(f"Batch call failed: {exc}")
                for payload in to_create + to_update:
                    report["errors"].append({"ref_art": payload["sku"], "error": str(exc)})
                results = {"create": [], "update": []}

            now = datetime.datetime.now().isoformat()
            for action, bucket in (("create", results["create"]), ("update", results["update"])):
                target_list = report["created"] if action == "create" else report["updated"]
                for row in bucket:
                    ref = row.get("sku")
                    if ref is None or ref not in pending:
                        continue
                    new_hash, image_source, new_anchor = pending[ref]
                    if row.get("error"):
                        store.record_error(ref, str(row["error"]), now)
                        report["errors"].append({"ref_art": ref, "error": row["error"]})
                        continue
                    store.upsert(ref, row.get("id"), new_hash, image_source, now,
                                 last_regular_price=new_anchor)
                    target_list.append(ref)

        # Orphans: articles the state store remembers syncing that no longer
        # appear in this pass (deleted, or REF_ART renamed -- see plan notes
        # on why renames aren't auto-merged). Safe to compute even dry-run
        # since it's a read-only comparison against the state store.
        report["orphans"] = sorted(store.all_ref_arts() - seen_refs)

    emit(f"Done. created={len(report['created'])} updated={len(report['updated'])} "
         f"unchanged={report['unchanged']} orphans={len(report['orphans'])} "
         f"errors={len(report['errors'])}")
    return report


DRY_RUN_OUTPUT_PATH = "dry_run_payloads.json"


def write_dry_run_payloads(report, path=DRY_RUN_OUTPUT_PATH):
    """Writes the full dry-run payload list to a JSON file for review --
    catalogs can run into the thousands of articles, too many to usefully
    print to a terminal or a GUI log view in full. Returns 'path' if
    something was written, else None (nothing to write)."""
    payloads = report.get("payloads")
    if not payloads:
        return None
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payloads, fh, ensure_ascii=False, indent=2, default=str)
    return path


def render_report_lines(report, payload_limit=None):
    """Human-readable detail lines for a run_sync() report -- the actual
    dry-run payloads, orphans, and errors. Used by both the CLI (--sync
    --dry-run, where this is the only way to see what would be sent) and
    the GUI's log view, so neither surface silently drops this detail.

    'payload_limit' caps how many payloads are rendered (catalogs can run
    into the thousands of articles, and dumping all of them to a terminal
    isn't useful) -- pass None to render every one (e.g. into a scrollable
    GUI log view)."""
    lines = []
    payloads = report.get("payloads") or []
    if payloads:
        shown = payloads if payload_limit is None else payloads[:payload_limit]
        lines.append(f"--- Dry-run payloads ({len(payloads)} total) ---")
        for item in shown:
            lines.append(f"[{item['action']}] {item['ref_art']}: {item['payload']}")
        if payload_limit is not None and len(payloads) > payload_limit:
            lines.append(f"... and {len(payloads) - payload_limit} more "
                         f"(see the full JSON output for the rest)")
    if report.get("orphans"):
        lines.append(f"Orphaned (previously synced, no longer found): {report['orphans']}")
    if report.get("errors"):
        lines.append(f"Errors: {report['errors']}")
    return lines
