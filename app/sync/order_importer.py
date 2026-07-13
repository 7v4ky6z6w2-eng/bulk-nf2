"""WooCommerce orders -> Firebird PIECE/ITEM documents (e.g. Bon de Livraison).

Ported from the user's existing wc_order_import.py, with one correctness
fix: uses the PROVEN Firebird generator pattern from import_bon_reception.py
(real generator names NEXTPIECE/NEXTITEM, a next-id = max(MAX(existing),
current generator value) base, with the generator explicitly advanced
afterwards) instead of the old script's generic/untested generator-name
guessing, which falls back to a bare MAX(NOPIECE)+1 that never advances
the real generator -- risking an ID collision with the ERP software's own
native document numbering later.
"""

import datetime
import logging

from app.db.firebird_client import connect as connect_firebird, python_codec_for
from app.sync.wc_orders_client import WCOrdersClient

log = logging.getLogger(__name__)

# Ignore reserved ID ranges (e.g. inventories) above this when computing
# MAX(existing) -- same threshold import_bon_reception.py uses.
RESERVED_ID_THRESHOLD = 1000000


def _parse_wc_date(s):
    if not s:
        return datetime.datetime.now()
    try:
        return datetime.datetime.fromisoformat(s.split("+")[0].split("Z")[0])
    except ValueError:
        return datetime.datetime.now()


# The exact "not cancelled" representation for PIECE.ANNULEE in this
# install isn't confirmed yet (numeric 0/1 vs. CHAR 'N'/'O' are both common
# in Firebird ERPs of this vintage). A SQL-side "ANNULEE = 0" filter was
# found to silently never match real rows -- which made already_imported()
# always return None and re-create every order on every run. Judging
# "cancelled" in Python against this permissive allowlist of "not
# cancelled" spellings is robust to either convention.
_NOT_ANNULLED_VALUES = {None, 0, "0", "N", "n", "", False}


def _is_annulled(value):
    return value not in _NOT_ANNULLED_VALUES


def _normalize_wc_after(date_str):
    """"YYYY-MM-DD" (from the GUI's date picker) -> the ISO8601 datetime
    WooCommerce's 'after' filter expects. Already-full datetimes pass
    through unchanged. Returns None for blank/unset (no date filter)."""
    date_str = (date_str or "").strip()
    if not date_str:
        return None
    return date_str if "T" in date_str else f"{date_str}T00:00:00"


class OrderImporter:
    def __init__(self, con, cfg):
        self.con = con
        self.cfg = cfg["order_import"]
        self.cur = con.cursor()
        self._codec = python_codec_for(cfg["firebird"].get("charset"))

    def _safe_text(self, s):
        """Replaces any character the connection's charset can't encode
        (e.g. a Kurdish/Persian letter with a WIN1256 connection) with '?'
        instead of letting the whole order fail on insert -- a customer's
        free-text name/address shouldn't be able to lose an entire order's
        stock movement over one unsupported character."""
        if not s:
            return s
        try:
            s.encode(self._codec)
            return s
        except (UnicodeEncodeError, LookupError):
            return s.encode(self._codec, errors="replace").decode(self._codec)

    # -- generator helpers (proven pattern from import_bon_reception.py) ----
    def _gen_value(self, generator):
        self.cur.execute(f"SELECT GEN_ID({generator}, 0) FROM RDB$DATABASE")
        return self.cur.fetchone()[0] or 0

    def _advance_generator(self, generator, target):
        current = self._gen_value(generator)
        if target > current:
            self.cur.execute(f"SELECT GEN_ID({generator}, {target - current}) FROM RDB$DATABASE")
            self.cur.fetchone()

    def _next_base(self, generator, table, col):
        self.cur.execute(
            f"SELECT MAX(CAST({col} AS BIGINT)) FROM {table} "
            f"WHERE {col} SIMILAR TO '[0-9]+' AND CHAR_LENGTH({col}) <= 15 "
            f"  AND CAST({col} AS BIGINT) < ?",
            (RESERVED_ID_THRESHOLD,),
        )
        mx = self.cur.fetchone()[0] or 0
        return max(int(mx), int(self._gen_value(generator)))

    # -- lookups --------------------------------------------------------------
    def verify_client(self):
        self.cur.execute(
            "SELECT RAISON_SOCIALE FROM TIERS WHERE CODE_TIERS = ?", (self.cfg["client_code"],)
        )
        row = self.cur.fetchone()
        return row[0].strip() if row and row[0] else None

    def already_imported(self, wc_order_id, doc_type):
        refdoc = f"WC-{wc_order_id}"
        self.cur.execute(
            "SELECT NOPIECE, ANNULEE FROM PIECE WHERE REFDOC = ? AND CODE_TYPE_PIECE = ?",
            (refdoc, doc_type),
        )
        row = self.cur.fetchone()
        if not row:
            return None
        nopiece, annulee = row
        return None if _is_annulled(annulee) else nopiece

    def find_source_piece(self, wc_order_id, source_type):
        refdoc = f"WC-{wc_order_id}"
        self.cur.execute(
            "SELECT NOPIECE, ANNULEE FROM PIECE WHERE REFDOC = ? AND CODE_TYPE_PIECE = ?",
            (refdoc, source_type),
        )
        row = self.cur.fetchone()
        if not row:
            return None
        nopiece, annulee = row
        return None if _is_annulled(annulee) else nopiece

    def lookup_article(self, sku):
        """Returns (REF_ART, PRIXVENTEHT, PRIXVENTETTC, TAUX_TVA) or None."""
        self.cur.execute(
            "SELECT REF_ART, PRIXVENTEHT, PRIXVENTETTC, TAUX_TVA FROM ARTICLE WHERE REF_ART = ?",
            (sku,),
        )
        row = self.cur.fetchone()
        if not row:
            return None
        ref, prix_ht, prix_ttc, tva = row
        return (
            str(ref).strip(),
            float(prix_ht) if prix_ht is not None else None,
            float(prix_ttc) if prix_ttc is not None else None,
            float(tva) if tva is not None else 0.0,
        )

    # -- main entry -----------------------------------------------------------
    def import_order(self, wc_order, dry_run=False, wc_orders_client=None):
        """Returns ('created'|'skipped'|'error', message, reason).

        'reason' is a stable machine-readable code the caller can tally
        (e.g. to show "N already imported" separately from "N status not
        mapped" in a summary) -- notably 'already_imported', which is the
        direct evidence the REFDOC duplicate check is doing its job on a
        re-run over the same order history."""
        order_id = wc_order["id"]
        wc_status = (wc_order.get("status") or "").lower()

        doc_type = self.cfg["status_mapping"].get(wc_status)
        if not doc_type:
            return "skipped", f"status '{wc_status}' not mapped", "status_not_mapped"

        existing = self.already_imported(order_id, doc_type)
        if existing:
            return ("skipped", f"{doc_type} already imported as NOPIECE={existing}",
                    "already_imported")

        billing = wc_order.get("billing") or {}
        shipping = wc_order.get("shipping") or {}
        addr_src = shipping if shipping.get("address_1") else billing
        full_name = " ".join(filter(None, [
            addr_src.get("first_name", ""), addr_src.get("last_name", ""),
        ])).strip()
        address = ", ".join(filter(None, [
            addr_src.get("address_1", ""), addr_src.get("address_2", ""),
            addr_src.get("city", ""), addr_src.get("postcode", ""),
        ]))
        phone = (billing.get("phone") or "").strip()
        full_name = self._safe_text(full_name)
        address = self._safe_text(address)
        phone = self._safe_text(phone)
        date_piece = _parse_wc_date(wc_order.get("date_created"))

        line_payloads = []
        total_ht = total_ttc = 0.0
        skipped_lines = 0

        for li in wc_order.get("line_items", []):
            sku = (li.get("sku") or "").strip()
            qte = float(li.get("quantity") or 0)
            if qte <= 0:
                skipped_lines += 1
                continue

            if not sku and wc_orders_client is not None:
                pid = li.get("product_id")
                vid = li.get("variation_id")
                if vid and pid:
                    sku = wc_orders_client.get_variation_sku(pid, vid)
                if not sku and pid:
                    sku = wc_orders_client.get_product_sku(pid)

            if not sku:
                log.warning("Order %s: line with no resolvable SKU (product_id=%s), skipped",
                            order_id, li.get("product_id"))
                skipped_lines += 1
                continue

            art = self.lookup_article(sku)
            if not art:
                if self.cfg["on_missing_sku"] == "skip_order":
                    return ("error", f"SKU {sku} not found in Firebird ARTICLE (skip_order policy)",
                            "missing_sku")
                log.warning("Order %s: SKU %s not in Firebird ARTICLE, line skipped", order_id, sku)
                skipped_lines += 1
                continue

            ref_art, price_ht_db, price_ttc_db, tva_rate = art
            price_ttc = price_ttc_db if price_ttc_db else float(li.get("price") or 0)
            if price_ht_db is not None:
                price_ht = price_ht_db
            elif tva_rate:
                price_ht = price_ttc / (1 + tva_rate / 100.0)
            else:
                price_ht = price_ttc

            total_ht += price_ht * qte
            total_ttc += price_ttc * qte
            line_payloads.append({
                "ref_art": ref_art, "qte": qte,
                "prixht": round(price_ht, 4), "prixttc": round(price_ttc, 4),
                "tva": tva_rate,
            })

        if not line_payloads:
            return "skipped", "no valid lines", "no_valid_lines"

        total_ht = round(total_ht, 2)
        total_ttc = round(total_ttc, 2)
        total_tva = round(total_ttc - total_ht, 2)

        source_type = self.cfg["transformation"].get(doc_type)
        source_nopiece = self.find_source_piece(order_id, source_type) if source_type else None

        if dry_run:
            link = f" (would link to {source_type}#{source_nopiece})" if source_nopiece else ""
            return ("skipped", (f"[DRY] -> {doc_type}: {len(line_payloads)} line(s), "
                                 f"HT={total_ht}, TTC={total_ttc}{link}"), "dry_run")

        refdoc = f"WC-{order_id}"
        nopiece = str(self._next_base("NEXTPIECE", "PIECE", "NOPIECE") + 1)
        self.cur.execute(
            "INSERT INTO PIECE "
            "(NOPIECE, NOPIECE_O, CODE_TYPE_PIECE, CODE_TIERS, DATEPIECE, REFDOC, "
            " USERNAME, MONTANTHT, MONTANTTTC, TVA, CODE_DEPOT, NOM_CONTACT, "
            " LIVR_ADRESSE, LIVR_TELEPHONE, LIVR_DATE, ANNULEE) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0)",
            (nopiece, source_nopiece, doc_type, self.cfg["client_code"], date_piece, refdoc,
             self.cfg["username"], total_ht, total_ttc, total_tva,
             self.cfg["code_depot"] or None,
             full_name[:100] if full_name else None,
             address[:200] if address else None,
             phone[:50] if phone else None,
             date_piece),
        )
        self._advance_generator("NEXTPIECE", int(nopiece))

        item_no = self._next_base("NEXTITEM", "ITEM", "NOITEM")
        for lp in line_payloads:
            item_no += 1
            self.cur.execute(
                "INSERT INTO ITEM "
                "(NOITEM, NOPIECE, REF_ART, QTE, PRIXHT, PRIXTTC, TVA, DATEPIECE, "
                " CODE_TIERS, CODE_DEPOT, ANNULEE) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0)",
                (str(item_no), nopiece, lp["ref_art"], lp["qte"], lp["prixht"], lp["prixttc"],
                 lp["tva"], date_piece, self.cfg["client_code"], self.cfg["code_depot"] or None),
            )
        self._advance_generator("NEXTITEM", item_no)

        if source_nopiece:
            self.cur.execute(
                "UPDATE PIECE SET NOPIECE_T = ? WHERE NOPIECE = ?", (nopiece, source_nopiece)
            )

        self.con.commit()
        extra = f" (skipped {skipped_lines} line(s))" if skipped_lines else ""
        link = f" linked from {source_type}#{source_nopiece}" if source_nopiece else ""
        return ("created", f"{doc_type} NOPIECE={nopiece}, {len(line_payloads)} line(s){extra}{link}",
                None)

    # -- cancellation -----------------------------------------------------------
    def cancel_order(self, wc_order, dry_run=False):
        """Annuls (ANNULEE=1) every still-active PIECE/ITEM document already
        created for this order, instead of creating anything new. Used for
        orders whose WooCommerce status is in cfg['cancel_statuses'] -- e.g.
        an order that went processing (document created) -> cancelled.

        Returns ('cancelled'|'skipped'|'error', message, reason)."""
        order_id = wc_order["id"]
        refdoc = f"WC-{order_id}"
        self.cur.execute(
            "SELECT NOPIECE, CODE_TYPE_PIECE, ANNULEE FROM PIECE WHERE REFDOC = ?",
            (refdoc,),
        )
        rows = self.cur.fetchall()
        if not rows:
            return "skipped", "no existing document to cancel", "no_existing_document"

        active = [(nopiece, doc_type) for nopiece, doc_type, annulee in rows if not _is_annulled(annulee)]
        if not active:
            return "skipped", "already cancelled", "already_cancelled"

        docs = ", ".join(f"{doc_type}#{nopiece}" for nopiece, doc_type in active)
        if dry_run:
            return "skipped", f"[DRY] would annul {docs}", "dry_run"

        for nopiece, _doc_type in active:
            self.cur.execute("UPDATE PIECE SET ANNULEE = 1 WHERE NOPIECE = ?", (nopiece,))
            self.cur.execute("UPDATE ITEM SET ANNULEE = 1 WHERE NOPIECE = ?", (nopiece,))
        self.con.commit()
        return "cancelled", f"annulled {docs}", None


def run_order_import(cfg, dry_run=False, log_fn=None):
    """Scans the FULL WooCommerce order history matching the configured
    statuses every time it runs -- there's no separate "past orders" mode,
    because this already is one: the REFDOC duplicate check
    (OrderImporter.already_imported) makes it safe to re-run over orders
    already imported, so a first run naturally backfills all matching
    history and every later run only picks up what's new.

    If cfg['order_import']['start_date'] is set ("YYYY-MM-DD"), only
    orders created on/after that date are fetched at all -- useful to
    keep a large store's every-run scan fast, or to deliberately exclude
    old orders from ever being imported.

    Orders whose WooCommerce status is in cfg['order_import']['cancel_statuses']
    are handled separately: instead of creating a document, any document(s)
    already created for that order (by an earlier run, back when it had a
    mapped status) get annulled -- e.g. an order that went
    processing -> cancelled.

    Returns a report dict:
    {"created": [order_id, ...], "cancelled": [order_id, ...],
     "skipped": [order_id, ...],
     "skip_reasons": {"already_imported": N, "status_not_mapped": N,
                       "no_valid_lines": N, "dry_run": N,
                       "no_existing_document": N, "already_cancelled": N},
     "errors": [{"order_id": ..., "error": ...}]}
    'skip_reasons["already_imported"]' is the direct, visible count of
    duplicates the tool caught and did NOT re-create.
    """
    emit = log_fn or (lambda msg: log.info(msg))
    oi_cfg = cfg["order_import"]

    def record(order_id, status, msg, reason):
        emit(f"WC#{order_id}: {status} - {msg}")
        if status in ("created", "cancelled"):
            report[status].append(order_id)
        elif status == "skipped":
            report["skipped"].append(order_id)
            if reason:
                report["skip_reasons"][reason] = report["skip_reasons"].get(reason, 0) + 1
        else:
            report["errors"].append({"order_id": order_id, "error": msg})

    con = connect_firebird(cfg)
    report = {"created": [], "cancelled": [], "skipped": [], "skip_reasons": {}, "errors": []}
    try:
        importer = OrderImporter(con, cfg)
        client_name = importer.verify_client()
        if not client_name:
            raise RuntimeError(f"Client {oi_cfg['client_code']!r} not found in TIERS")
        emit(f"Client OK: {oi_cfg['client_code']} = {client_name}")

        create_statuses = list(oi_cfg["status_mapping"].keys())
        cancel_statuses = list(oi_cfg.get("cancel_statuses") or [])
        if not create_statuses and not cancel_statuses:
            raise RuntimeError("No status_mapping or cancel_statuses configured -- nothing to import")
        cancel_set = {s.strip().lower() for s in cancel_statuses if s.strip()}

        wc_cfg = cfg["woocommerce"]
        wc_orders = WCOrdersClient(wc_cfg["site_url"], wc_cfg["consumer_key"], wc_cfg["consumer_secret"])
        all_statuses = list(dict.fromkeys(create_statuses + cancel_statuses))
        after = _normalize_wc_after(oi_cfg.get("start_date"))
        orders = wc_orders.fetch_orders(all_statuses, after=after)
        scope = f"created on/after {oi_cfg['start_date']}" if after else "full history, not just new ones"
        emit(f"{len(orders)} order(s) fetched matching configured statuses ({scope}).")

        for order in orders:
            order_id = order["id"]
            wc_status = (order.get("status") or "").lower()
            try:
                if wc_status in cancel_set:
                    status, msg, reason = importer.cancel_order(order, dry_run=dry_run)
                else:
                    status, msg, reason = importer.import_order(
                        order, dry_run=dry_run, wc_orders_client=wc_orders
                    )
                record(order_id, status, msg, reason)
            except Exception as exc:  # noqa: BLE001 -- one bad order shouldn't kill the run
                con.rollback()
                report["errors"].append({"order_id": order_id, "error": str(exc)})
    finally:
        con.close()

    reasons = ", ".join(f"{k}={v}" for k, v in report["skip_reasons"].items())
    emit(f"Done. created={len(report['created'])} cancelled={len(report['cancelled'])} "
         f"skipped={len(report['skipped'])} ({reasons}) errors={len(report['errors'])}")
    return report
