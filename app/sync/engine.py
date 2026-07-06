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

    fields = {
        "sku": article["ref_art"],
        "name": article["designation"],
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
    if barcodes:
        fields["global_unique_id"] = barcodes[0]
        meta_data.append({"key": "_barcode", "value": barcodes[0]})
        if len(barcodes) > 1:
            meta_data.append({"key": "_alt_barcodes", "value": ",".join(barcodes[1:])})
    if meta_data:
        fields["meta_data"] = meta_data

    fields["_category_name"] = _category_from_designation(article["designation"])
    return fields


def _category_from_designation(designation):
    """WooCommerce category name: the first word of the article's
    DESIGNATION, Title-cased, so "stylo bille bleu" / "STYLO ..." / "Stylo
    ..." all land in the same "Stylo" category instead of near-duplicates
    piling up from inconsistent data-entry casing."""
    if not designation:
        return None
    first_word = designation.strip().split()
    if not first_word:
        return None
    return first_word[0].capitalize()


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

        for article in articles:
            ref = article["ref_art"]
            seen_refs.add(ref)
            core = build_core_fields(article, cfg)

            image = None
            if cfg["sync"]["sync_images"]:
                image = extract_image(article["photo"], ref)

            new_hash = content_hash(core, has_image=bool(image),
                                     image_bytes=image.data if image else None)
            existing = store.get(ref)

            if existing and existing["content_hash"] == new_hash:
                report["unchanged"] += 1
                continue

            payload = {k: v for k, v in core.items() if not k.startswith("_")}

            if dry_run:
                payload["categories"] = [{"name": core["_category_name"]}] if core["_category_name"] else []
                payload["images"] = ["<would upload ARTICLE.PHOTO>"] if image else []
                report["payloads"].append({"ref_art": ref, "payload": payload,
                                            "action": "update" if existing else "create"})
                continue

            if core["_category_name"]:
                try:
                    cat_id = wc_client.find_or_create_category(core["_category_name"])
                    payload["categories"] = [{"id": cat_id}]
                except WooCommerceError as exc:
                    report["errors"].append({"ref_art": ref, "error": f"category: {exc}"})

            image_source = "none"
            if image:
                try:
                    media_id = wc_client.upload_media(image.data, image.filename, image.mime_type)
                    payload["images"] = [{"id": media_id}]
                    image_source = "blob"
                except WooCommerceError as exc:
                    report["errors"].append({"ref_art": ref, "error": f"image: {exc}"})

            pending[ref] = (new_hash, image_source)
            if existing and existing["wc_product_id"]:
                payload["id"] = existing["wc_product_id"]
                to_update.append(payload)
            else:
                to_create.append(payload)

        if not dry_run and (to_create or to_update):
            try:
                results = wc_client.batch_products(create=to_create, update=to_update)
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
                    new_hash, image_source = pending[ref]
                    if row.get("error"):
                        store.record_error(ref, str(row["error"]), now)
                        report["errors"].append({"ref_art": ref, "error": row["error"]})
                        continue
                    store.upsert(ref, row.get("id"), new_hash, image_source, now)
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
