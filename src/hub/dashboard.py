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


@bp.get("/ventes")
def ventes():
    con = _db()
    rows = con.execute(
        "SELECT p.store_id, p.datepiece, p.code_tiers, t.raison_sociale, "
        "       p.montantttc, p.code_mode_regl, p.nopiece "
        "FROM piece p LEFT JOIN tiers t "
        "  ON p.store_id=t.store_id AND p.code_tiers=t.code_tiers "
        "WHERE p.datepiece >= date('now','-7 days') "
        "ORDER BY p.datepiece DESC LIMIT 300").fetchall()
    names = _store_names()
    return render_template("ventes.html", rows=[dict(r) for r in rows], store_names=names)


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
