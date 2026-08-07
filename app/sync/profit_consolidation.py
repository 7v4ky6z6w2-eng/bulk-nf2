"""Consolidate WooCommerce deliveries into one cost-basis document, to see
real profit.

Every WooCommerce order gets imported (by order_importer.py) as a Bon de
Livraison under one dedicated placeholder client (cfg["order_import"]
["client_code"]) -- what the user calls "the livraison client". Those
documents are priced at the normal SALE price, same as any other delivery.

This module answers "how much profit did I actually make through
WooCommerce": it gathers every not-yet-consolidated Bon de Livraison made
for that client, aggregates their lines by REF_ART, and creates ONE new
document under a client the user picks, priced at PURCHASE price
(ARTICLE.PRIXACHATHT/TTC) instead -- i.e. 0% margin. Comparing that
document's total against the sum of the original (sale-price) documents it
came from gives the real profit for that period.

Two-step, interactive workflow (the user explicitly wants to review
anything that looks wrong before it's written):
  1. list_source_document_types() -- shows every CODE_TYPE_PIECE found
     under the source client with counts, so the user can identify which
     one is the real Bon de Livraison. An order typically also creates an
     intermediate "Commande" document under the SAME client (see
     order_import's "transformation" config, which links a BL back to its
     source commande) -- consolidating that one too would double-count
     every item. This can't be safely auto-detected, so the user picks it.
  2. preview_consolidation() -- gathers candidates for the chosen type,
     splits them into "ready" (clean cost data) and "held back" (an item
     with no matching ARTICLE row, or ARTICLE.PRIXACHATHT/TTC = 0/NULL --
     the two things that would silently corrupt a profit number). Writes
     nothing.
  3. run_consolidation() -- re-gathers the same data (never stale relative
     to what was previewed) and, for the "ready" set only, creates one
     PIECE/ITEM document. A held-back document is left completely alone
     (not marked consolidated) so it's picked up automatically once the
     user fixes the underlying ARTICLE row and re-runs -- deliberately
     all-or-nothing per source document, never split across two runs.

Correctness note on COEFF/COEFF_TR: this consolidated document is a
read-only report, not a real second delivery of goods that were already
delivered once. Its PIECE/ITEM COEFF and COEFF_TR are always written as 0
(not looked up from LOCAL_TYPE_PIECE like a normal document would be) so
it does NOT double the source deliveries' stock deduction and does NOT
post anything to the chosen client's real balance -- see
app/sync/order_importer.py's _type_piece_coefficients docstring for how
NetFact2 computes both of those live from COEFF/COEFF_TR. The document
still displays normally (correct totals, ANNULEE=1/not cancelled) so it
can be opened/printed in NetFact2 like any other Bon de Livraison; it just
has zero side effects beyond existing as a readable record.

A local SQLite table (see StateStore.get_consolidated_source_nopieces /
mark_consolidated) remembers which source NOPIECEs have already been
folded into a previous consolidated document, so running this again later
(e.g. monthly) only pulls in NEW deliveries -- never double-counts.
"""

import datetime
import logging

from app.db.firebird_client import connect as connect_firebird
from app.db.piece_writer import PieceWriter
from app.sync.order_importer import _is_annulled
from app.sync.state_store import StateStore

log = logging.getLogger(__name__)

REF_ART_CHUNK = 500  # defensive batching for the ARTICLE IN (...) lookup


def _parse_since(since):
    """"YYYY-MM-DD" -> datetime.date, or None for no filter."""
    since = (since or "").strip()
    if not since:
        return None
    return datetime.datetime.strptime(since, "%Y-%m-%d").date()


def list_source_document_types(cfg, source_client_code, log_fn=None):
    """Every CODE_TYPE_PIECE found under the source client, with counts and
    totals -- lets the user identify which one is the real Bon de
    Livraison before consolidating anything. See the module docstring for
    why this can't be auto-detected safely."""
    emit = log_fn or (lambda msg: log.info(msg))
    con = connect_firebird(cfg)
    try:
        cur = con.cursor()
        cur.execute(
            "SELECT p.CODE_TYPE_PIECE, t.INTITULE, p.NOPIECE, p.MONTANT, p.ANNULEE "
            "FROM PIECE p LEFT JOIN LOCAL_TYPE_PIECE t ON t.CODE_TYPE_PIECE = p.CODE_TYPE_PIECE "
            "WHERE p.CODE_TIERS = ?",
            (source_client_code,),
        )
        rows = cur.fetchall()
    finally:
        con.close()

    with StateStore(cfg["state_db_path"]) as store:
        already_done = store.get_consolidated_source_nopieces()

    by_type = {}
    for code_type_piece, intitule, nopiece, montant, annulee in rows:
        if _is_annulled(annulee):
            continue
        entry = by_type.setdefault(code_type_piece, {
            "code_type_piece": code_type_piece,
            "intitule": (intitule or "").strip(),
            "count": 0, "total_montant": 0.0, "already_consolidated": 0,
        })
        entry["count"] += 1
        entry["total_montant"] += float(montant or 0)
        if nopiece in already_done:
            entry["already_consolidated"] += 1

    result = sorted(by_type.values(), key=lambda e: e["code_type_piece"])
    if not result:
        emit(f"No active documents found for client {source_client_code!r}.")
    for e in result:
        emit(f"{e['code_type_piece']} ({e['intitule'] or 'no label'}): {e['count']} document(s), "
             f"total {round(e['total_montant'], 2)}, {e['already_consolidated']} already consolidated")
    return result


def _fetch_candidate_docs(cur, source_client_code, doc_type_code, since):
    sql = ("SELECT p.NOPIECE, p.DATEPIECE, p.ANNULEE, i.REF_ART, i.QTE, "
           "       i.PRIXHT, i.PRIXTTC, i.ANNULEE "
           "FROM PIECE p JOIN ITEM i ON i.NOPIECE = p.NOPIECE "
           "WHERE p.CODE_TIERS = ? AND p.CODE_TYPE_PIECE = ?")
    params = [source_client_code, doc_type_code]
    since_date = _parse_since(since)
    if since_date:
        sql += " AND p.DATEPIECE >= ?"
        params.append(since_date)
    sql += " ORDER BY p.NOPIECE"
    cur.execute(sql, params)

    docs = {}
    for nopiece, datepiece, p_annulee, ref_art, qte, prixht, prixttc, i_annulee in cur.fetchall():
        if _is_annulled(p_annulee) or _is_annulled(i_annulee):
            continue
        doc = docs.setdefault(nopiece, {"nopiece": nopiece, "date": datepiece, "items": []})
        doc["items"].append({
            "ref_art": (ref_art or "").strip(),
            "qte": float(qte or 0),
            "prixht": float(prixht) if prixht is not None else 0.0,
            "prixttc": float(prixttc) if prixttc is not None else 0.0,
        })
    return [d for d in docs.values() if d["items"]]


def _fetch_articles_cost(cur, refs):
    if not refs:
        return {}
    result = {}
    refs = list(refs)
    for i in range(0, len(refs), REF_ART_CHUNK):
        chunk = refs[i:i + REF_ART_CHUNK]
        placeholders = ",".join("?" for _ in chunk)
        cur.execute(
            f"SELECT REF_ART, PRIXACHATHT, PRIXACHATTTC, TAUX_TVA FROM ARTICLE "
            f"WHERE REF_ART IN ({placeholders})",
            chunk,
        )
        for ref_art, pa_ht, pa_ttc, tva in cur.fetchall():
            result[str(ref_art).strip()] = {
                "prix_achat_ht": float(pa_ht) if pa_ht is not None else 0.0,
                "prix_achat_ttc": float(pa_ttc) if pa_ttc is not None else 0.0,
                "taux_tva": float(tva) if tva is not None else 0.0,
            }
    return result


def _resolve_cost(art):
    """Returns (cost_ht, cost_ttc), filling in whichever side is missing
    from the other via the article's tax rate -- same fallback pattern
    order_importer.import_order() uses for sale price. Returns None if
    both purchase-price fields are 0/blank -- the "zero_cost" flag case."""
    ht = art["prix_achat_ht"]
    ttc = art["prix_achat_ttc"]
    tva = art["taux_tva"]
    if not ht and not ttc:
        return None
    if ht and not ttc:
        ttc = ht * (1 + tva / 100.0)
    elif ttc and not ht:
        ht = ttc / (1 + tva / 100.0) if tva else ttc
    return (round(ht, 4), round(ttc, 4))


def _classify(cur, docs, already_done):
    """Splits candidate source documents into "ready" (clean cost data,
    aggregated by REF_ART) and "held back" (flagged issue -- see module
    docstring). All-or-nothing per document: one bad line holds back the
    whole document, not just that line."""
    pending_docs = [d for d in docs if d["nopiece"] not in already_done]
    already_consolidated_skipped = len(docs) - len(pending_docs)

    all_refs = {item["ref_art"] for d in pending_docs for item in d["items"] if item["ref_art"]}
    articles = _fetch_articles_cost(cur, all_refs)
    cost_cache = {}

    def resolved_cost(ref):
        if ref not in cost_cache:
            art = articles.get(ref)
            cost_cache[ref] = _resolve_cost(art) if art else None
        return cost_cache[ref]

    ready_lines = {}
    ready_nopieces = []
    ready_original_total_ht = 0.0
    ready_original_total_ttc = 0.0
    held_back = []

    for d in pending_docs:
        flags = []
        seen = set()
        for item in d["items"]:
            ref = item["ref_art"]
            if not ref:
                key = ("(blank SKU)", "missing_sku")
            elif ref not in articles:
                key = (ref, "missing_ref")
            elif resolved_cost(ref) is None:
                key = (ref, "zero_cost")
            else:
                continue
            if key not in seen:
                seen.add(key)
                flags.append({"ref_art": key[0], "reason": key[1]})

        if flags:
            held_back.append({"nopiece": d["nopiece"], "date": d["date"], "flags": flags})
            continue

        ready_nopieces.append(d["nopiece"])
        for item in d["items"]:
            ref = item["ref_art"]
            cost_ht, cost_ttc = resolved_cost(ref)
            line = ready_lines.setdefault(ref, {
                "qty": 0.0, "cost_ht": cost_ht, "cost_ttc": cost_ttc,
                "tva": articles[ref]["taux_tva"],
            })
            line["qty"] += item["qte"]
            ready_original_total_ht += item["prixht"] * item["qte"]
            ready_original_total_ttc += item["prixttc"] * item["qte"]

    return {
        "ready_lines": ready_lines,
        "ready_source_nopieces": ready_nopieces,
        "ready_source_count": len(ready_nopieces),
        "ready_original_total_ht": round(ready_original_total_ht, 2),
        "ready_original_total_ttc": round(ready_original_total_ttc, 2),
        "held_back": held_back,
        "already_consolidated_skipped": already_consolidated_skipped,
    }


def _cost_totals(ready_lines):
    total_ht = sum(v["cost_ht"] * v["qty"] for v in ready_lines.values())
    total_ttc = sum(v["cost_ttc"] * v["qty"] for v in ready_lines.values())
    return round(total_ht, 2), round(total_ttc, 2)


def preview_consolidation(cfg, source_client_code, doc_type_code, since=None, log_fn=None):
    """Dry-run: gathers and classifies candidates, writes nothing. Returns
    a report dict -- see run_consolidation() for the shared shape (minus
    "created_nopiece", which is always None here)."""
    emit = log_fn or (lambda msg: log.info(msg))
    con = connect_firebird(cfg)
    try:
        cur = con.cursor()
        docs = _fetch_candidate_docs(cur, source_client_code, doc_type_code, since)
        with StateStore(cfg["state_db_path"]) as store:
            already_done = store.get_consolidated_source_nopieces()
        result = _classify(cur, docs, already_done)
    finally:
        con.close()

    total_cost_ht, total_cost_ttc = _cost_totals(result["ready_lines"])
    profit_ht = round(result["ready_original_total_ht"] - total_cost_ht, 2)
    profit_ttc = round(result["ready_original_total_ttc"] - total_cost_ttc, 2)

    emit(f"[DRY] {result['ready_source_count']} delivery document(s) ready to consolidate "
         f"({result['already_consolidated_skipped']} already consolidated previously, "
         f"{len(result['held_back'])} held back).")
    if result["ready_lines"]:
        emit(f"{len(result['ready_lines'])} distinct article(s): "
             f"cost HT={total_cost_ht} / TTC={total_cost_ttc}, "
             f"original sale HT={result['ready_original_total_ht']} / TTC={result['ready_original_total_ttc']} "
             f"-> estimated profit HT={profit_ht} / TTC={profit_ttc}")
    for h in result["held_back"]:
        reasons = ", ".join(f"{f['ref_art']} ({f['reason']})" for f in h["flags"])
        emit(f"HELD BACK doc #{h['nopiece']} ({h['date']}): {reasons}")

    return {
        "created_nopiece": None,
        "source_doc_count": result["ready_source_count"],
        "line_count": len(result["ready_lines"]),
        "already_consolidated_skipped": result["already_consolidated_skipped"],
        "total_cost_ht": total_cost_ht, "total_cost_ttc": total_cost_ttc,
        "original_sale_ht": result["ready_original_total_ht"],
        "original_sale_ttc": result["ready_original_total_ttc"],
        "estimated_profit_ht": profit_ht, "estimated_profit_ttc": profit_ttc,
        "held_back": result["held_back"],
    }


def run_consolidation(cfg, source_client_code, doc_type_code, target_client_code,
                       since=None, dry_run=False, log_fn=None):
    """Creates one consolidated, cost-priced PIECE/ITEM document under
    target_client_code from every not-yet-consolidated Bon de Livraison
    found under source_client_code. See the module docstring for the full
    workflow and the COEFF=0 correctness note. Returns a report dict (see
    preview_consolidation() for the shared shape)."""
    if dry_run:
        return preview_consolidation(cfg, source_client_code, doc_type_code, since, log_fn)

    emit = log_fn or (lambda msg: log.info(msg))
    con = connect_firebird(cfg)
    report = {
        "created_nopiece": None, "source_doc_count": 0, "line_count": 0,
        "already_consolidated_skipped": 0,
        "total_cost_ht": 0.0, "total_cost_ttc": 0.0,
        "original_sale_ht": 0.0, "original_sale_ttc": 0.0,
        "estimated_profit_ht": 0.0, "estimated_profit_ttc": 0.0,
        "held_back": [],
    }
    try:
        cur = con.cursor()

        cur.execute("SELECT RAISON_SOCIALE FROM TIERS WHERE CODE_TIERS = ?", (target_client_code,))
        row = cur.fetchone()
        if not row:
            raise RuntimeError(f"Target client {target_client_code!r} not found in TIERS")
        emit(f"Target client OK: {target_client_code} = {(row[0] or '').strip()}")

        docs = _fetch_candidate_docs(cur, source_client_code, doc_type_code, since)
        with StateStore(cfg["state_db_path"]) as store:
            already_done = store.get_consolidated_source_nopieces()
            result = _classify(cur, docs, already_done)

            report["held_back"] = result["held_back"]
            report["source_doc_count"] = result["ready_source_count"]
            report["already_consolidated_skipped"] = result["already_consolidated_skipped"]
            report["line_count"] = len(result["ready_lines"])

            if not result["ready_lines"]:
                emit("Nothing new to consolidate.")
                return report

            total_cost_ht, total_cost_ttc = _cost_totals(result["ready_lines"])
            report.update({
                "total_cost_ht": total_cost_ht, "total_cost_ttc": total_cost_ttc,
                "original_sale_ht": result["ready_original_total_ht"],
                "original_sale_ttc": result["ready_original_total_ttc"],
                "estimated_profit_ht": round(result["ready_original_total_ht"] - total_cost_ht, 2),
                "estimated_profit_ttc": round(result["ready_original_total_ttc"] - total_cost_ttc, 2),
            })

            pw = PieceWriter(cur)
            now = datetime.datetime.now()
            refdoc = f"COGS-{now.strftime('%Y%m%d%H%M%S')}"
            nopiece = str(pw.next_base("NEXTPIECE", "PIECE", "NOPIECE") + 1)
            total_tva = round(total_cost_ttc - total_cost_ht, 2)
            # COEFF/COEFF_TR are deliberately 0, not looked up from
            # LOCAL_TYPE_PIECE -- see the module docstring's correctness
            # note on why this document must not double the source
            # deliveries' stock effect or post to the target client's
            # real balance.
            cur.execute(
                "INSERT INTO PIECE "
                "(NOPIECE, CODE_TYPE_PIECE, CODE_TIERS, DATEPIECE, REFDOC, USERNAME, "
                " MONTANTHT, MONTANTTTC, MONTANT, COEFF, COEFF_TR, TVA, CODE_DEPOT, ANNULEE) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 0, ?, ?, 1)",
                (nopiece, doc_type_code, target_client_code, now, refdoc,
                 "PROFIT_CONSOLIDATION", total_cost_ht, total_cost_ttc, total_cost_ttc,
                 total_tva, cfg["order_import"]["code_depot"] or None),
            )
            pw.advance_generator("NEXTPIECE", int(nopiece))

            item_no = pw.next_base("NEXTITEM", "ITEM", "NOITEM")
            for ref_art in sorted(result["ready_lines"]):
                line = result["ready_lines"][ref_art]
                item_no += 1
                cur.execute(
                    "INSERT INTO ITEM "
                    "(NOITEM, NOPIECE, REF_ART, QTE, PRIXHT, PRIXTTC, COEFF, COEFF_TR, TVA, "
                    " DATEPIECE, CODE_TIERS, CODE_DEPOT, ANNULEE) "
                    "VALUES (?, ?, ?, ?, ?, ?, 0, 0, ?, ?, ?, ?, 1)",
                    (str(item_no), nopiece, ref_art, line["qty"], line["cost_ht"], line["cost_ttc"],
                     line["tva"], now, target_client_code, cfg["order_import"]["code_depot"] or None),
                )
            pw.advance_generator("NEXTITEM", item_no)

            con.commit()
            store.mark_consolidated(result["ready_source_nopieces"], doc_type_code, nopiece,
                                     now.isoformat())
            report["created_nopiece"] = nopiece
    finally:
        con.close()

    if report["created_nopiece"]:
        emit(f"Done. Created {doc_type_code} NOPIECE={report['created_nopiece']} for "
             f"{target_client_code}: {report['line_count']} line(s) from "
             f"{report['source_doc_count']} source document(s). "
             f"Cost HT={report['total_cost_ht']} / TTC={report['total_cost_ttc']}. "
             f"Original sale HT={report['original_sale_ht']} / TTC={report['original_sale_ttc']}. "
             f"Estimated profit HT={report['estimated_profit_ht']} / TTC={report['estimated_profit_ttc']}.")
    if report["held_back"]:
        emit(f"{len(report['held_back'])} document(s) held back due to flagged issues -- "
             f"fix and re-run to include them.")
    return report
