"""Reconcile Yalidine (courier) delivery data against NetFact2's
"livraison" client, to find where a reported total gap actually comes
from -- order by order, not just as one mismatched number.

Every order dispatched to Yalidine (via the site's Yalidine WordPress
plugin) carries a tracking number on WC_Order meta '_yalidine_tracking',
and NetFact2's own delivery document for that same order is
PIECE.REFDOC = 'WC-<order id>' under the dedicated "livraison" client
(cfg["order_import"]["client_code"], same client order_importer.py uses)
-- both already keyed by the WooCommerce order id, so that's the join key
here too; no new correlation scheme needed.

Comparison is Yalidine's DECLARED price at dispatch time (the COD amount
sent when the parcel was created, and what /parcels/?tracking=... returns
back as 'price') vs NetFact2's PIECE.MONTANT for that REFDOC under the
livraison client's real delivery document type (as opposed to any
intermediate "Commande" also created under the same client -- see
profit_consolidation.py's module docstring for why that distinction
matters; the caller must pass the same doc_type_code used there).

Every order is bucketed into exactly one outcome:
  - "matched": Yalidine shows it delivered, NetFact2 has the document,
    amounts agree (within a small rounding tolerance). Counted in the
    totals but not listed individually.
  - "amount_mismatch": both sides have it, but the amounts disagree.
  - "missing_in_netfact": Yalidine shows it delivered, but there's no
    matching PIECE at all -- e.g. the order_importer never created one
    (missing SKU, status not mapped, an error). This money is real
    (Yalidine says it was delivered) but never became NetFact2 revenue.
  - "missing_in_yalidine": NetFact2 has the livraison document, but the
    order was never dispatched to Yalidine (no tracking at all) -- this
    money is recorded as revenue but was never actually shipped/collected
    by this courier (through this integration, at least).
  - "returned_or_failed_but_billed": Yalidine's status says the parcel
    was returned/failed/cancelled, yet NetFact2 still has an active
    livraison document (and its MONTANT) for it -- money that was likely
    never actually collected but is still sitting in NetFact2 as if it
    was delivered.
  - orders still in transit are counted but not itemized -- there's
    nothing to reconcile yet.

Read-only: this tool never writes to Firebird, WooCommerce, or Yalidine.
"""

import logging
import re
import unicodedata

import requests

from app.db.firebird_client import connect as connect_firebird
from app.sync.order_importer import _is_annulled
from app.sync.wc_orders_client import WCOrdersClient

log = logging.getLogger(__name__)

YALIDINE_BASE_URL = "https://api.yalidine.app/v1/"
TRACKING_CHUNK = 50
AMOUNT_TOLERANCE = 1.0  # DA -- integer-ish amounts on both sides, allow rounding slack


class YalidineError(Exception):
    pass


class YalidineClient:
    def __init__(self, api_id, api_token, timeout=30):
        self.api_id = api_id
        self.api_token = api_token
        self.timeout = timeout

    def _get(self, endpoint, params=None):
        try:
            resp = requests.get(
                YALIDINE_BASE_URL + endpoint, params=params, timeout=self.timeout,
                headers={"X-API-ID": self.api_id, "X-API-TOKEN": self.api_token},
            )
        except requests.RequestException as exc:
            raise YalidineError(str(exc)) from exc
        if resp.status_code >= 400:
            raise YalidineError(f"GET {endpoint} -> {resp.status_code}: {resp.text[:300]}")
        return resp.json()

    def fetch_parcels_by_tracking(self, trackings, log_fn=None):
        """Returns {tracking: {"status": str, "price": float|None}}. A
        chunk that fails is logged and skipped rather than aborting the
        whole reconciliation -- a transient Yalidine error shouldn't hide
        every other order's result."""
        emit = log_fn or (lambda msg: log.info(msg))
        result = {}
        trackings = list(trackings)
        for i in range(0, len(trackings), TRACKING_CHUNK):
            chunk = trackings[i:i + TRACKING_CHUNK]
            try:
                body = self._get("parcels/", params={"tracking": ",".join(chunk)})
            except YalidineError as exc:
                emit(f"Yalidine lookup failed for {len(chunk)} tracking(s): {exc}")
                continue
            for row in (body.get("data") or []):
                tr = str(row.get("tracking") or "").strip()
                if not tr:
                    continue
                price = row.get("price")
                result[tr] = {
                    "status": row.get("last_status") or "",
                    "price": float(price) if price is not None else None,
                }
        return result


def _strip_accents(s):
    return "".join(c for c in unicodedata.normalize("NFD", s) if unicodedata.category(c) != "Mn")


def _is_delivered(status):
    return bool(re.search(r"\blivre(e)?\b", _strip_accents((status or "").lower())))


def _is_returned_or_failed(status):
    normalized = _strip_accents((status or "").lower())
    return any(k in normalized for k in ("retour", "echec", "annul"))


def _extract_tracking(order):
    for m in order.get("meta_data") or []:
        if m.get("key") == "_yalidine_tracking":
            return str(m.get("value") or "").strip()
    return ""


def _normalize_after(since):
    since = (since or "").strip()
    if not since:
        return None
    return since if "T" in since else f"{since}T00:00:00"


def _fetch_netfact_totals(cfg, livraison_client_code, doc_type_code):
    """{REFDOC: total_active_MONTANT} for every 'WC-...' document of this
    type under this client -- summed rather than taken from a single row,
    in case fix_duplicate_orders() hasn't been run yet and a REFDOC still
    has more than one row."""
    con = connect_firebird(cfg)
    try:
        cur = con.cursor()
        cur.execute(
            "SELECT REFDOC, MONTANT, ANNULEE FROM PIECE "
            "WHERE CODE_TIERS = ? AND CODE_TYPE_PIECE = ? AND REFDOC STARTING WITH 'WC-'",
            (livraison_client_code, doc_type_code),
        )
        totals = {}
        for refdoc, montant, annulee in cur.fetchall():
            if _is_annulled(annulee):
                continue
            totals[refdoc] = totals.get(refdoc, 0.0) + float(montant or 0)
        return totals
    finally:
        con.close()


def run_reconciliation(cfg, livraison_client_code, doc_type_code, since=None, log_fn=None):
    """Returns a report dict:
    {"orders_checked": N, "dispatched_to_yalidine": N,
     "matched_count": N, "matched_total": float,
     "amount_mismatch": [...], "missing_in_netfact": [...],
     "missing_in_yalidine": [...], "returned_or_failed_but_billed": [...],
     "in_transit_count": N,
     "net_gap": float}
    'net_gap' is the signed total of every discrepancy bucket -- positive
    means Yalidine/NetFact2 combined shows MORE money owed to NetFact2
    than is currently recorded there; negative means NetFact2 has revenue
    on the books that this courier integration can't account for."""
    emit = log_fn or (lambda msg: log.info(msg))
    yal_cfg = cfg["yalidine"]
    if not yal_cfg.get("api_id") or not yal_cfg.get("api_token"):
        raise YalidineError("Yalidine API ID/token are not configured.")

    wc_cfg = cfg["woocommerce"]
    wc_orders = WCOrdersClient(wc_cfg["site_url"], wc_cfg["consumer_key"], wc_cfg["consumer_secret"])
    orders = wc_orders.fetch_all_orders(after=_normalize_after(since))
    emit(f"{len(orders)} WooCommerce order(s) fetched.")

    tracking_by_order = {}
    for o in orders:
        tr = _extract_tracking(o)
        if tr:
            tracking_by_order[o["id"]] = tr
    emit(f"{len(tracking_by_order)} order(s) were dispatched to Yalidine (have a tracking number).")

    yalidine = YalidineClient(yal_cfg["api_id"], yal_cfg["api_token"])
    parcels = yalidine.fetch_parcels_by_tracking(tracking_by_order.values(), log_fn=emit)
    emit(f"{len(parcels)} parcel(s) matched by tracking number in Yalidine.")

    netfact_totals = _fetch_netfact_totals(cfg, livraison_client_code, doc_type_code)
    emit(f"{len(netfact_totals)} NetFact2 '{doc_type_code}' document(s) found for the livraison client.")

    report = {
        "orders_checked": len(orders),
        "dispatched_to_yalidine": len(tracking_by_order),
        "matched_count": 0, "matched_total": 0.0,
        "amount_mismatch": [], "missing_in_netfact": [],
        "missing_in_yalidine": [], "returned_or_failed_but_billed": [],
        "in_transit_count": 0,
    }

    seen_refdocs = set()
    for order_id, tracking in tracking_by_order.items():
        parcel = parcels.get(tracking)
        if parcel is None:
            continue  # Yalidine lookup didn't return this one -- not enough info to judge
        refdoc = f"WC-{order_id}"
        seen_refdocs.add(refdoc)
        netfact_montant = netfact_totals.get(refdoc)
        status, price = parcel["status"], parcel["price"]

        if _is_delivered(status):
            if netfact_montant is None:
                report["missing_in_netfact"].append({
                    "order_id": order_id, "tracking": tracking, "yalidine_price": price,
                })
            elif price is not None and abs(price - netfact_montant) > AMOUNT_TOLERANCE:
                report["amount_mismatch"].append({
                    "order_id": order_id, "tracking": tracking,
                    "yalidine_price": price, "netfact_montant": netfact_montant,
                    "diff": round(price - netfact_montant, 2),
                })
            else:
                report["matched_count"] += 1
                report["matched_total"] += netfact_montant
        elif _is_returned_or_failed(status):
            if netfact_montant is not None:
                report["returned_or_failed_but_billed"].append({
                    "order_id": order_id, "tracking": tracking,
                    "status": status, "netfact_montant": netfact_montant,
                })
        else:
            report["in_transit_count"] += 1

    for refdoc, montant in netfact_totals.items():
        if refdoc not in seen_refdocs:
            report["missing_in_yalidine"].append({"refdoc": refdoc, "netfact_montant": montant})

    net_gap = (
        sum(m["diff"] for m in report["amount_mismatch"])
        + sum((m["yalidine_price"] or 0) for m in report["missing_in_netfact"])
        - sum(m["netfact_montant"] for m in report["missing_in_yalidine"])
        - sum(m["netfact_montant"] for m in report["returned_or_failed_but_billed"])
    )
    report["net_gap"] = round(net_gap, 2)

    emit(f"Done. matched={report['matched_count']} (total {round(report['matched_total'], 2)}) "
         f"amount_mismatch={len(report['amount_mismatch'])} "
         f"missing_in_netfact={len(report['missing_in_netfact'])} "
         f"missing_in_yalidine={len(report['missing_in_yalidine'])} "
         f"returned_but_billed={len(report['returned_or_failed_but_billed'])} "
         f"in_transit={report['in_transit_count']} "
         f"net_gap={report['net_gap']}")
    return report
