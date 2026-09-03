"""Routes Flask du tableau de bord web.

Surtout de la lecture (vue d'ensemble, stock, ventes, trésorerie, historique),
plus deux écritures ciblées : confirmer une correspondance manuelle entre
articles (aucune donnée métier modifiée, juste un regroupement d'affichage)
et synchroniser des prix de vente entre magasins, via le même submit_op que
l'app bureau et les pages mobiles.

Protégé par la MÊME session que les pages mobiles (/m/login, code d'accès) :
un navigateur non connecté est redirigé vers l'écran de connexion. Les agents
et l'app bureau ne passent pas par ici (ils utilisent /api/* avec X-Api-Key).
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta

from flask import Blueprint, jsonify, redirect, render_template, request, \
    session, url_for, current_app

from hub.central_db import (
    tresorerie_today, tresorerie_caisses, store_status, stock_search,
    ops_history, name_match_suggestions, confirm_article_link,
    price_sync_candidates, ArticleLinkConflict,
    stock_value, stock_value_top, low_stock, negative_stock, transfer_suggestions,
    sales_by_product, dead_stock, VENTE_TYPES,
    piece_type_breakdown, tresorerie_range_by_store,
    fournisseur_mapping, fournisseur_set_mapping, fournisseur_delete_mapping,
    fournisseur_pending_list, fournisseur_pending_get, fournisseur_pending_resolve,
    fournisseur_pending_ignore, fournisseur_sync_state_set,
    fournisseur_settings_get, fournisseur_settings_set,
    fournisseur_store_settings_get, fournisseur_store_settings_set,
    fournisseur_receptions_history, fournisseur_reception_lines,
    fournisseur_sync_state_clear_by_dest, fournisseur_pending_creation_clear,
)
from hub.ops import submit_op

bp = Blueprint("dashboard", __name__)


@bp.before_request
def _require_login():
    # Même session que /m/ ; si aucun code n'est configuré, accès libre
    # (cohérent avec le mode « ouvert » de l'API).
    has_code = bool(current_app.config.get("API_KEY")
                    or current_app.config.get("ACCESS_CODE"))
    if has_code and not session.get("mobile_auth"):
        return redirect(url_for("mobile.login_form", next=request.path))
    return None


def _db() -> sqlite3.Connection:
    return current_app.config["db_connection"]()


def _store_names() -> dict:
    return current_app.config.get("store_names", {})


def _registry():
    return current_app.config.get("registry")


def _ago(ts: str | None) -> str:
    if not ts:
        return "jamais"
    try:
        dt = datetime.fromisoformat(ts)
        delta = datetime.now(dt.tzinfo) - dt
        minutes = int(delta.total_seconds() // 60)
        if minutes < 2:
            return "à l'instant"
        if minutes < 60:
            return "il y a %dm" % minutes
        hours = minutes // 60
        if hours < 24:
            return "il y a %dh" % hours
        return "il y a %dj" % (hours // 24)
    except Exception:  # noqa: BLE001
        return ts or "?"


@bp.get("/")
def overview():
    statuses = store_status(_db())
    names = _store_names()
    # Entrées / solde du jour par magasin (affichés sur les cartes).
    money: dict = {}
    for r in tresorerie_today(_db()):
        m = money.setdefault(r["store_id"], {"entree": 0.0, "sortie": 0.0})
        val = float(r.get("total_encaisse") or 0)
        m["sortie" if r.get("sens") == "sortie" else "entree"] += val
    for s in statuses:
        s["name"] = names.get(s["store_id"], s.get("store_name") or "Magasin %d" % s["store_id"])
        s["last_ok_ago"] = _ago(s.get("last_ok"))
        s["last_seen_ago"] = _ago(s.get("last_seen"))
        m = money.get(s["store_id"], {"entree": 0.0, "sortie": 0.0})
        s["entree"] = m["entree"]
        s["solde"] = m["entree"] - m["sortie"]
    return render_template("overview.html", stores=statuses)


@bp.get("/tresorerie")
def tresorerie():
    from flask import request
    day = datetime.now().strftime("%Y-%m-%d")
    caisse = request.args.get("caisse") or None
    rows = tresorerie_today(_db(), day, caisse)
    # Liste des caisses pour le sélecteur
    caisses = sorted({c["caisse"] for c in tresorerie_caisses(_db())
                      if c.get("caisse") and c["caisse"] != "(globale)"})
    names = _store_names()
    by_store: dict = {}
    for r in rows:
        sid = r["store_id"]
        st = by_store.setdefault(sid, {
            "name": names.get(sid, "Magasin %d" % sid),
            "entree": 0.0, "sortie": 0.0,
            "modes": {},
            "synced_at": _ago(r.get("synced_at")),
        })
        val = float(r.get("total_encaisse") or 0)
        mode = r.get("mode_paiement", "?")
        m = st["modes"].setdefault(mode, {"mode": mode, "entree": 0.0, "sortie": 0.0})
        if r.get("sens") == "sortie":
            st["sortie"] += val; m["sortie"] += val
        else:
            st["entree"] += val; m["entree"] += val
    for st in by_store.values():
        st["solde"] = st["entree"] - st["sortie"]
        st["modes"] = list(st["modes"].values())
    tot_entree = sum(s["entree"] for s in by_store.values())
    tot_sortie = sum(s["sortie"] for s in by_store.values())
    return render_template("tresorerie.html", stores=by_store.values(),
                           tot_entree=tot_entree, tot_sortie=tot_sortie,
                           solde=tot_entree - tot_sortie, day=day,
                           caisses=caisses, selected_caisse=(caisse or ""))


@bp.get("/stock")
def stock():
    # Une ligne par PRODUIT (regroupé par match_key, comme l'Éditeur de prix),
    # une colonne par MAGASIN — jamais un total combiné entre magasins : le
    # but est justement de voir le détail par magasin.
    q = request.args.get("q", "").strip()
    con = _db()
    rows = stock_search(con, q, limit=300)
    names = _store_names()
    reg = _registry()
    store_ids = [s.id for s in reg.stores] if reg else \
        sorted({r["store_id"] for r in rows})

    by_key: dict = {}
    for r in rows:
        key = r.get("match_key") or r["ref_art"]
        g = by_key.setdefault(key, {"desig": r.get("designation"), "refs": {}, "qty": {}})
        g["refs"][r["store_id"]] = r["ref_art"]
        g["qty"][r["store_id"]] = r.get("qte_stock")
    groups = []
    for g in by_key.values():
        refs = sorted({g["refs"][sid] for sid in store_ids if sid in g["refs"]})
        g["ref_txt"] = refs[0] if len(refs) == 1 else " / ".join(refs)
        groups.append(g)
    groups.sort(key=lambda g: g["ref_txt"])
    return render_template("stock.html", groups=groups, store_ids=store_ids,
                           store_names=names, q=q)


@bp.get("/correspondances")
def correspondances():
    groups = name_match_suggestions(_db())
    return render_template("correspondances.html", groups=groups,
                           store_names=_store_names(), error=None)


@bp.post("/correspondances/confirmer")
def correspondances_confirm():
    members = [{"store_id": int(sid), "ref_art": ref}
              for sid, ref in zip(request.form.getlist("store_id"),
                                  request.form.getlist("ref_art"))
              if sid and ref]
    error = None
    try:
        confirm_article_link(_db(), members)
    except ArticleLinkConflict as exc:
        error = str(exc)
    groups = name_match_suggestions(_db())
    return render_template("correspondances.html", groups=groups,
                           store_names=_store_names(), error=error)


def _price_sync_rows(con, source_store_id: int) -> list:
    rows = []
    for g in price_sync_candidates(con, source_store_id):
        for t in g["targets"]:
            rows.append({"designation": g["designation"], "source_price": g["source_price"],
                         "store_id": t["store_id"], "ref_art": t["ref_art"],
                         "current_price": t["current_price"]})
    return rows


@bp.get("/synchroniser-prix")
def synchroniser_prix():
    reg = _registry()
    stores = reg.stores if reg else []
    source_arg = request.args.get("source")
    source = int(source_arg) if source_arg else (stores[0].id if stores else None)
    rows = _price_sync_rows(_db(), source) if source else []
    return render_template("synchroniser_prix.html", stores=stores, source=source,
                           rows=rows, store_names=_store_names(), results=None)


@bp.post("/synchroniser-prix/appliquer")
def synchroniser_prix_appliquer():
    source = int(request.form.get("source") or 0)
    reg = _registry()
    names = _store_names()
    con = _db()
    results = []
    for idx in request.form.getlist("rows"):
        sid_s = request.form.get("store_id_%s" % idx)
        ref = request.form.get("ref_art_%s" % idx)
        price_s = request.form.get("price_%s" % idx)
        if not sid_s or not ref or not price_s:
            continue
        sid, price = int(sid_s), float(price_s)
        changes = [{"ref0": ref, "values": {"PRIXVENTEHT": price, "PRIXVENTETTC": price}}]
        res = submit_op(con, reg, sid, "price_update", {"changes": changes})
        res.update(store_id=sid, store_name=names.get(sid, "Magasin %s" % sid), ref_art=ref)
        results.append(res)

    stores = reg.stores if reg else []
    rows = _price_sync_rows(con, source) if source else []
    return render_template("synchroniser_prix.html", stores=stores, source=source,
                           rows=rows, store_names=names, results=results)


@bp.get("/stock-valeur")
def stock_valeur():
    con = _db()
    names = _store_names()
    reg = _registry()
    summary = stock_value(con)
    for s in summary:
        s["name"] = names.get(s["store_id"], "Magasin %d" % s["store_id"])
    summary.sort(key=lambda s: -s["valeur"])
    store_id_arg = request.args.get("store_id")
    store_id = int(store_id_arg) if store_id_arg else None
    top = stock_value_top(con, store_id)
    stores = reg.stores if reg else []
    return render_template("stock_valeur.html", summary=summary, top=top,
                           stores=stores, store_id=store_id, store_names=names)


@bp.get("/stock-bas")
def stock_bas():
    con = _db()
    try:
        seuil = max(0, int(request.args.get("seuil", 3)))
    except ValueError:
        seuil = 3
    rows = low_stock(con, seuil)
    return render_template("stock_bas.html", rows=rows, seuil=seuil,
                           store_names=_store_names())


@bp.get("/stock-negatif")
def stock_negatif():
    con = _db()
    rows = negative_stock(con)
    return render_template("stock_negatif.html", rows=rows, store_names=_store_names())


@bp.get("/transferts")
def transferts():
    con = _db()
    rows = transfer_suggestions(con)
    return render_template("transferts.html", rows=rows, store_names=_store_names())


@bp.get("/ventes")
def ventes():
    con = _db()
    vente_sql = "(" + ",".join("'%s'" % t for t in VENTE_TYPES) + ")"
    rows = con.execute(
        "SELECT p.store_id, p.datepiece, p.code_tiers, t.raison_sociale, "
        "       p.montantttc, p.code_mode_regl, p.nopiece "
        "FROM piece p LEFT JOIN tiers t "
        "  ON p.store_id=t.store_id AND p.code_tiers=t.code_tiers "
        "WHERE p.datepiece >= date('now','-7 days') "
        "  AND p.code_type_piece IN " + vente_sql + " "
        "ORDER BY p.datepiece DESC LIMIT 300").fetchall()
    names = _store_names()
    return render_template("ventes.html", rows=[dict(r) for r in rows], store_names=names)


@bp.get("/ventes-produits")
def ventes_produits():
    con = _db()
    reg = _registry()
    try:
        days = max(1, int(request.args.get("days", 30)))
    except ValueError:
        days = 30
    order = "asc" if request.args.get("order") == "asc" else "desc"
    store_id_arg = request.args.get("store_id")
    store_id = int(store_id_arg) if store_id_arg else None
    rows = sales_by_product(con, days, store_id, order)
    stores = reg.stores if reg else []
    return render_template("ventes_produits.html", rows=rows, days=days, order=order,
                           stores=stores, store_id=store_id, store_names=_store_names())


@bp.get("/stock-mort")
def stock_mort():
    con = _db()
    reg = _registry()
    try:
        days = max(1, int(request.args.get("days", 30)))
    except ValueError:
        days = 30
    store_id_arg = request.args.get("store_id")
    store_id = int(store_id_arg) if store_id_arg else None
    rows = dead_stock(con, days, store_id)
    stores = reg.stores if reg else []
    return render_template("stock_mort.html", rows=rows, days=days,
                           stores=stores, store_id=store_id, store_names=_store_names())


@bp.get("/ventes-comparaison")
def ventes_comparaison():
    # Même source et même méthode que la Vue d'ensemble (tresorerie_snapshot,
    # entrées/sorties de caisse réelles) — pas la table PIECE — étalée sur une
    # période au lieu d'aujourd'hui seulement. Chaque magasin peut avoir sa
    # propre caisse à retenir (ex. un magasin où une seule caisse sur
    # plusieurs est fiable) via le sélecteur par magasin.
    con = _db()
    reg = _registry()
    names = _store_names()
    today = datetime.now().strftime("%Y-%m-%d")
    date_from = request.args.get("from") or (datetime.now() - timedelta(days=6)).strftime("%Y-%m-%d")
    date_to = request.args.get("to") or today

    store_ids = [s.id for s in reg.stores] if reg else []
    caisses_by_store: dict = {}
    for row in tresorerie_caisses(con):
        caisses_by_store.setdefault(row["store_id"], []).append(row["caisse"])
    if not store_ids:
        store_ids = sorted(caisses_by_store)

    selected_caisse: dict = {}
    caisse_by_store: dict = {}
    for sid in store_ids:
        val = (request.args.get("caisse_%d" % sid) or "").strip()
        selected_caisse[sid] = val
        if val:
            caisse_by_store[sid] = val

    days_sorted = tresorerie_range_by_store(con, date_from, date_to, caisse_by_store)
    totals = {sid: sum(d["par_magasin"].get(sid, {}).get("entree", 0) for d in days_sorted)
             for sid in store_ids}
    return render_template("ventes_comparaison.html", days=days_sorted, store_ids=store_ids,
                           totals=totals, date_from=date_from, date_to=date_to,
                           store_names=names, caisses_by_store=caisses_by_store,
                           selected_caisse=selected_caisse)


@bp.get("/diagnostic-types")
def diagnostic_types():
    rows = piece_type_breakdown(_db())
    return render_template("diagnostic_types.html", rows=rows, vente_types=list(VENTE_TYPES),
                           store_names=_store_names())


# ── Synchro fournisseur : mapping client -> magasin, et file d'attente ───────
@bp.get("/fournisseur-mapping")
def fournisseur_mapping_view():
    con = _db()
    reg = _registry()
    mapping = fournisseur_mapping(con)
    settings = fournisseur_settings_get(con)
    stores = reg.stores if reg else []
    store_settings = {s.id: fournisseur_store_settings_get(con, s.id) for s in stores}
    return render_template("fournisseur_mapping.html", mapping=mapping, settings=settings,
                           stores=stores, store_names=_store_names(),
                           store_settings=store_settings)


@bp.post("/fournisseur-mapping/set")
def fournisseur_mapping_set():
    code_tiers = (request.form.get("code_tiers") or "").strip()
    store_id = request.form.get("store_id")
    raison = (request.form.get("raison_sociale") or "").strip() or None
    if code_tiers and store_id:
        fournisseur_set_mapping(_db(), code_tiers, int(store_id), raison)
    return redirect(url_for("dashboard.fournisseur_mapping_view"))


@bp.post("/fournisseur-mapping/settings")
def fournisseur_settings_set_route():
    code_type_piece_reception = (request.form.get("code_type_piece_reception") or "").strip()
    fournisseur_settings_set(_db(), code_type_piece_reception)
    return redirect(url_for("dashboard.fournisseur_mapping_view"))


@bp.post("/fournisseur-mapping/store-settings")
def fournisseur_store_settings_set_route():
    try:
        store_id = int(request.form.get("store_id") or 0)
    except ValueError:
        store_id = 0
    code_tiers = (request.form.get("code_tiers") or "").strip()
    code_depot = (request.form.get("code_depot") or "").strip()
    if store_id:
        fournisseur_store_settings_set(_db(), store_id, code_tiers, code_depot)
    return redirect(url_for("dashboard.fournisseur_mapping_view"))


@bp.post("/fournisseur-mapping/delete")
def fournisseur_mapping_delete():
    code_tiers = (request.form.get("code_tiers") or "").strip()
    if code_tiers:
        fournisseur_delete_mapping(_db(), code_tiers)
    return redirect(url_for("dashboard.fournisseur_mapping_view"))


@bp.get("/fournisseur-pending")
def fournisseur_pending_view():
    pending = fournisseur_pending_list(_db())
    return render_template("fournisseur_pending.html", pending=pending,
                           store_names=_store_names())


@bp.post("/fournisseur-pending/resolve")
def fournisseur_pending_resolve_route():
    from hub.fournisseur import apply_new_line
    con = _db()
    reg = _registry()
    try:
        pending_id = int(request.form.get("pending_id") or 0)
    except ValueError:
        pending_id = 0
    action = request.form.get("action") or ""
    item = fournisseur_pending_get(con, pending_id) if pending_id else None
    error = None

    if not item or item["status"] != "pending":
        error = "Ligne introuvable ou déjà traitée."
    elif action == "ignore":
        fournisseur_pending_ignore(con, pending_id)
    else:
        dest_ref = (request.form.get("link_ref") or "").strip() if action == "link" \
            else (item.get("ref_art") or "").strip()
        if not dest_ref:
            error = "Référence manquante."
        else:
            result = apply_new_line(con, reg, item["store_id"], item["src_nopiece"],
                                    item["src_noitem"], dest_ref, item)
            if result.get("status") == "error":
                error = result.get("error")
            else:
                fournisseur_pending_resolve(con, pending_id,
                                            {"action": action, "ref_art": dest_ref,
                                             "result": result.get("status")})

    pending = fournisseur_pending_list(con)
    return render_template("fournisseur_pending.html", pending=pending,
                           store_names=_store_names(), error=error)


@bp.get("/fournisseur-historique")
def fournisseur_history_view():
    con = _db()
    history = fournisseur_receptions_history(con)
    return render_template("fournisseur_history.html", history=history,
                           store_names=_store_names())


@bp.get("/fournisseur-historique/detail")
def fournisseur_history_detail():
    con = _db()
    try:
        store_id = int(request.args.get("store_id") or 0)
    except ValueError:
        store_id = 0
    dest_nopiece = request.args.get("dest_nopiece") or ""
    lines = fournisseur_reception_lines(con, store_id, dest_nopiece) if store_id and dest_nopiece else []
    return jsonify(lines)


@bp.post("/fournisseur-historique/annuler")
def fournisseur_history_cancel():
    con = _db()
    reg = _registry()
    try:
        store_id = int(request.form.get("store_id") or 0)
    except ValueError:
        store_id = 0
    dest_nopiece = (request.form.get("dest_nopiece") or "").strip()
    error = None
    if not store_id or not dest_nopiece:
        error = "Réception introuvable."
    else:
        result = submit_op(con, reg, store_id, "cancel_piece", {"nopiece": dest_nopiece})
        if result.get("status") == "error":
            error = result.get("error")
        elif result.get("status") == "applied":
            # Annulée tout de suite (magasin en ligne) : purger le suivi
            # pour que cette ligne de BL soit retraitée comme neuve si le
            # fournisseur la redonne encore (voir docstring de la fonction).
            srcs = fournisseur_sync_state_clear_by_dest(con, store_id, dest_nopiece)
            for src in srcs:
                fournisseur_pending_creation_clear(con, store_id, src)
        # Si "queued" (magasin hors ligne) : l'annulation partira dès son
        # retour en ligne, mais fournisseur_sync_state reste en l'état tant
        # qu'on ne sait pas si l'annulation a réellement été appliquée —
        # évite de rouvrir la ligne comme "neuve" pour rien si l'agent est
        # simplement débranché quelques minutes.

    history = fournisseur_receptions_history(con)
    return render_template("fournisseur_history.html", history=history,
                           store_names=_store_names(), error=error)


@bp.get("/sync")
def sync_status():
    con = _db()
    logs = con.execute(
        "SELECT l.id, l.store_id, m.store_name, l.started, l.finished, "
        "       l.rows_pushed, l.status, l.error_msg "
        "FROM sync_log l LEFT JOIN store_meta m ON l.store_id=m.store_id "
        "ORDER BY l.id DESC LIMIT 50").fetchall()
    names = _store_names()
    return render_template("sync_status.html", logs=[dict(r) for r in logs],
                           store_names=names)


@bp.get("/historique")
def historique():
    rows = ops_history(_db())
    names = _store_names()
    return render_template("historique.html", rows=rows, store_names=names)


# API JSON (pour la page bureau qui rafraîchit sans rechargement)
@bp.get("/api/dashboard/tresorerie")
def api_tresorerie():
    day = datetime.now().strftime("%Y-%m-%d")
    return jsonify(rows=tresorerie_today(_db(), day), day=day)


@bp.get("/api/dashboard/stores")
def api_stores():
    return jsonify(stores=store_status(_db()))
