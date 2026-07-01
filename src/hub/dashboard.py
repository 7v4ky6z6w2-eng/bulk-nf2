"""Routes Flask du tableau de bord web (lecture seule)."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta

from flask import Blueprint, jsonify, render_template, current_app

from hub.central_db import tresorerie_today, tresorerie_caisses, store_status

bp = Blueprint("dashboard", __name__)


def _db() -> sqlite3.Connection:
    return current_app.config["db_connection"]()


def _store_names() -> dict:
    return current_app.config.get("store_names", {})


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
    for s in statuses:
        s["name"] = names.get(s["store_id"], s.get("store_name") or "Magasin %d" % s["store_id"])
        s["last_ok_ago"] = _ago(s.get("last_ok"))
        s["last_seen_ago"] = _ago(s.get("last_seen"))
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
    con = _db()
    rows = con.execute(
        "SELECT a.store_id, a.ref_art, a.designation, s.code_depot, s.qte_stock "
        "FROM stock_snapshot s JOIN article a "
        "  ON s.store_id=a.store_id AND s.ref_art=a.ref_art "
        "ORDER BY a.store_id, a.ref_art LIMIT 500").fetchall()
    names = _store_names()
    return render_template("stock.html", rows=[dict(r) for r in rows], store_names=names)


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


# API JSON (pour la page bureau qui rafraîchit sans rechargement)
@bp.get("/api/dashboard/tresorerie")
def api_tresorerie():
    day = datetime.now().strftime("%Y-%m-%d")
    return jsonify(rows=tresorerie_today(_db(), day), day=day)


@bp.get("/api/dashboard/stores")
def api_stores():
    return jsonify(stores=store_status(_db()))
