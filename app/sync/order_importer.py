"""WooCommerce orders -> Firebird PIECE/ITEM documents (e.g. Bon de Livraison).

Ported from the user's existing wc_order_import.py, with one correctness
fix: uses the PROVEN Firebird generator pattern from import_bon_reception.py
(real generator names NEXTPIECE/NEXTITEM, a next-id = max(MAX(existing),
current generator value) base, with the generator explicitly advanced
afterwards) instead of the old script's generic/untested generator-name
guessing, which falls back to a bare MAX(NOPIECE)+1 that never advances
the real generator -- risking an ID collision with the ERP software's own
native document numbering later.

Also sets PIECE.MONTANT/COEFF/COEFF_TR and ITEM.COEFF/COEFF_TR on every
insert (see OrderImporter._type_piece_coefficients), which the old script
never did. Confirmed from NetFact2's own embedded SQL that the client
balance (TIERS "solde") is computed live as
SUM(PIECE.MONTANT * PIECE.ANNULEE * (PIECE.COEFF + PIECE.COEFF_TR)) --
without these fields every imported order silently contributed 0 to the
client's balance, and to computed stock via the same COEFF pattern on
ITEM, regardless of ANNULEE.
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


# CONFIRMED against the live NetFact2 install (user cross-checked a raw
# ANNULEE=0 count against NetFact2's own "cancelled documents" count --
# exact match): ANNULEE=1 means NOT cancelled (the normal/valid state),
# ANNULEE=0 means cancelled. This is the OPPOSITE of what the column's
# name suggests and of every earlier assumption in this file -- every
# document this tool created before this fix was inserted with ANNULEE=0
# and so was cancelled from the moment of creation, and cancel_order()/
# fix_duplicate_orders() writing ANNULEE=1 were doing the opposite of
# cancelling. See repair_reversed_annulee() below for fixing rows
# already written with the old, backwards value.
_NOT_ANNULLED_VALUES = {1, "1", True}


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
        self._coeff_cache = {}

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
        """Returns the NOPIECE of an active (non-cancelled) document for
        this order + doc type, or None. Must scan every matching row, not
        just the first one: an order can have more than one PIECE sharing
        the same REFDOC (e.g. a leftover duplicate from the historical
        duplicate-reimport bug that got manually cancelled in NetFact2
        instead of deleted) -- fetchone() with no ORDER BY could return
        that cancelled row first and wrongly conclude nothing was ever
        imported, creating yet another duplicate."""
        refdoc = f"WC-{wc_order_id}"
        self.cur.execute(
            "SELECT NOPIECE, ANNULEE FROM PIECE WHERE REFDOC = ? AND CODE_TYPE_PIECE = ?",
            (refdoc, doc_type),
        )
        for nopiece, annulee in self.cur.fetchall():
            if not _is_annulled(annulee):
                return nopiece
        return None

    def find_source_piece(self, wc_order_id, source_type):
        refdoc = f"WC-{wc_order_id}"
        self.cur.execute(
            "SELECT NOPIECE, ANNULEE FROM PIECE WHERE REFDOC = ? AND CODE_TYPE_PIECE = ?",
            (refdoc, source_type),
        )
        for nopiece, annulee in self.cur.fetchall():
            if not _is_annulled(annulee):
                return nopiece
        return None

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

    def _type_piece_coefficients(self, code_type_piece):
        """Returns (coeff_piece, coeff_piece_tr, coeff_item, coeff_item_tr)
        for this document type, cached per run.

        NetFact2's TIERS balance is computed live as
        SUM(PIECE.MONTANT * PIECE.ANNULEE * (PIECE.COEFF + PIECE.COEFF_TR))
        -- confirmed from the application's own embedded SQL. PIECE.COEFF/
        COEFF_TR (and ITEM.COEFF/COEFF_TR, used the same way by the stock
        ledger) are per-document-type values NetFact2's own UI copies from
        LOCAL_TYPE_PIECE.COEFF_PIECE/COEFF_PIECE_TR/COEFF_ITEM/COEFF_ITEM_TR
        when creating a document -- our INSERT used to leave them at their
        column default (0), which silently zeroed out both the balance and
        stock effect of every imported order regardless of ANNULEE. Looking
        this up live (instead of hardcoding an assumed sign) means it stays
        correct even if the coefficients differ per document type or change
        later, exactly mirroring what NetFact2 itself does on save.

        Falls back to (0, 0, 0, 0) with a warning if the type isn't found in
        LOCAL_TYPE_PIECE -- same as the previous (broken) behavior, so an
        unrecognized type can't make things worse, just doesn't fix them."""
        if code_type_piece in self._coeff_cache:
            return self._coeff_cache[code_type_piece]
        self.cur.execute(
            "SELECT COEFF_PIECE, COEFF_PIECE_TR, COEFF_ITEM, COEFF_ITEM_TR "
            "FROM LOCAL_TYPE_PIECE WHERE CODE_TYPE_PIECE = ?",
            (code_type_piece,),
        )
        row = self.cur.fetchone()
        if not row:
            log.warning("LOCAL_TYPE_PIECE has no row for CODE_TYPE_PIECE=%s -- "
                        "PIECE/ITEM COEFF will be 0, so this document won't affect "
                        "the client's balance or stock", code_type_piece)
            coeffs = (0, 0, 0, 0)
        else:
            coeffs = tuple(int(v) if v is not None else 0 for v in row)
        self._coeff_cache[code_type_piece] = coeffs
        return coeffs

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

        coeff_piece, coeff_piece_tr, coeff_item, coeff_item_tr = self._type_piece_coefficients(doc_type)

        refdoc = f"WC-{order_id}"
        nopiece = str(self._next_base("NEXTPIECE", "PIECE", "NOPIECE") + 1)
        self.cur.execute(
            "INSERT INTO PIECE "
            "(NOPIECE, NOPIECE_O, CODE_TYPE_PIECE, CODE_TIERS, DATEPIECE, REFDOC, "
            " USERNAME, MONTANTHT, MONTANTTTC, MONTANT, COEFF, COEFF_TR, TVA, "
            " CODE_DEPOT, NOM_CONTACT, LIVR_ADRESSE, LIVR_TELEPHONE, LIVR_DATE, ANNULEE) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)",
            (nopiece, source_nopiece, doc_type, self.cfg["client_code"], date_piece, refdoc,
             self.cfg["username"], total_ht, total_ttc, total_ttc, coeff_piece, coeff_piece_tr,
             total_tva, self.cfg["code_depot"] or None,
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
                "(NOITEM, NOPIECE, REF_ART, QTE, PRIXHT, PRIXTTC, COEFF, COEFF_TR, TVA, "
                " DATEPIECE, CODE_TIERS, CODE_DEPOT, ANNULEE) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)",
                (str(item_no), nopiece, lp["ref_art"], lp["qte"], lp["prixht"], lp["prixttc"],
                 coeff_item, coeff_item_tr, lp["tva"], date_piece, self.cfg["client_code"],
                 self.cfg["code_depot"] or None),
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
        """Annuls (ANNULEE=0 -- see the confirmed-convention note above
        _NOT_ANNULLED_VALUES) every still-active PIECE/ITEM document
        already created for this order, instead of creating anything new.
        Used for orders whose WooCommerce status is in
        cfg['cancel_statuses'] -- e.g. an order that went processing
        (document created) -> cancelled.

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
            self.cur.execute("UPDATE PIECE SET ANNULEE = 0 WHERE NOPIECE = ?", (nopiece,))
            self.cur.execute("UPDATE ITEM SET ANNULEE = 0 WHERE NOPIECE = ?", (nopiece,))
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


def fix_duplicate_orders(cfg, dry_run=True, log_fn=None):
    """One-off cleanup for orders that ended up with more than one PIECE
    document for the same REFDOC ('WC-<order id>') + CODE_TYPE_PIECE --
    the historical duplicate-reimport bug (now fixed in
    already_imported()/find_source_piece() above) let this happen before
    the fix landed.

    Counts EVERY row regardless of ANNULEE, not just active ones: an
    order the user already cancelled the extra copy of by hand (one
    active + one cancelled reversal record) is still two documents for
    one WooCommerce order, and the goal is exactly one document per
    order -- not "at most one active document". Keeps one row per group
    (prefers an active one if any exist, else the earliest by NOPIECE)
    and DELETES the rest -- their ITEM rows first (they reference
    NOPIECE), then the PIECE row itself. This is a real SQL DELETE, not
    an annul: the user confirmed these extra rows are pure duplicate-bug
    artifacts they want gone, not kept around as cancelled records.

    Returns a report dict:
    {"fixed": [{"refdoc":.., "code_type_piece":.., "kept": nopiece,
                 "deleted": [nopiece, ...]}, ...],
     "total_deleted": N}
    """
    emit = log_fn or (lambda msg: log.info(msg))
    con = connect_firebird(cfg)
    report = {"fixed": [], "total_deleted": 0}
    try:
        cur = con.cursor()
        cur.execute(
            "SELECT REFDOC, CODE_TYPE_PIECE, NOPIECE, ANNULEE FROM PIECE "
            "WHERE REFDOC STARTING WITH 'WC-' ORDER BY REFDOC, CODE_TYPE_PIECE"
        )
        groups = {}
        for refdoc, doc_type, nopiece, annulee in cur.fetchall():
            groups.setdefault((refdoc, doc_type), []).append((nopiece, annulee))

        for (refdoc, doc_type), rows in groups.items():
            if len(rows) <= 1:
                continue
            active = sorted((n for n, a in rows if not _is_annulled(a)), key=lambda n: int(n))
            keep = active[0] if active else sorted((n for n, _a in rows), key=lambda n: int(n))[0]
            extras = sorted((n for n, _a in rows if n != keep), key=lambda n: int(n))
            emit(f"{refdoc} / {doc_type}: keep {keep}, delete {extras}"
                 + (" [DRY]" if dry_run else ""))
            if not dry_run:
                for nopiece in extras:
                    cur.execute("DELETE FROM ITEM WHERE NOPIECE = ?", (nopiece,))
                    cur.execute("DELETE FROM PIECE WHERE NOPIECE = ?", (nopiece,))
                con.commit()
            report["fixed"].append({"refdoc": refdoc, "code_type_piece": doc_type,
                                     "kept": keep, "deleted": extras})
            report["total_deleted"] += len(extras)
    finally:
        con.close()

    emit(f"Done. {len(report['fixed'])} order(s) with duplicates, "
         f"{report['total_deleted']} duplicate document(s) deleted.")
    return report
