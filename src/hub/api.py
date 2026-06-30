"""Endpoints Flask du hub : réception des données agents + pending_ops."""

from __future__ import annotations

import json
import sqlite3

from flask import Blueprint, jsonify, request, current_app

from hub.central_db import (
    upsert_batch, start_sync, finish_sync, add_rows_pushed,
    pending_ops_for, mark_op, enqueue_op, SYNC_TABLES,
    store_status, tresorerie_today, stock_rows, ventes_rows,
    sync_logs, pending_ops_recent, article_search,
)
from hub.write_back import is_reachable, write_bdr, write_prices, WriteError

bp = Blueprint("api", __name__)


def _db() -> sqlite3.Connection:
    return current_app.config["db_connection"]()


def _check_key():
    """Accepte la clé API (agents) OU le code d'accès (appareils clients).

    Si aucun des deux n'est configuré sur le hub, l'accès est ouvert (utile en
    réseau privé Tailscale de confiance). Sinon le header X-Api-Key doit
    correspondre à l'un des deux.
    """
    api_key = current_app.config.get("API_KEY", "")
    code = current_app.config.get("ACCESS_CODE", "")
    valid = {k for k in (api_key, code) if k}
    if valid and request.headers.get("X-Api-Key") not in valid:
        return jsonify(error="Clé ou code d'accès invalide"), 403
    return None


# ── Santé ─────────────────────────────────────────────────────────────────────
@bp.get("/api/ping")
def ping():
    return jsonify(ok=True)


# ── Configuration client (liste des magasins SANS secrets) ───────────────────
#  Permet à un appareil de fonctionner avec seulement l'URL du hub + le code
#  d'accès, sans copier stores.json (donc sans les mots de passe Firebird).
@bp.get("/api/client/config")
def client_config():
    err = _check_key()
    if err:
        return err
    registry = current_app.config.get("registry")
    stores = []
    if registry:
        for s in registry.stores:
            stores.append({"id": s.id, "name": s.name, "host": s.host, "port": s.port})
    return jsonify(stores=stores)


# ── Sync : démarrage de session ───────────────────────────────────────────────
@bp.post("/api/sync/start")
def sync_start():
    err = _check_key()
    if err:
        return err
    data = request.get_json(force=True) or {}
    store_id = int(data.get("store_id", 0))
    store_name = data.get("store_name", "")
    if not store_id:
        return jsonify(error="store_id manquant"), 400
    con = _db()
    session_id = start_sync(con, store_id, store_name)
    return jsonify(session_id=session_id)


# ── Sync : push de données ────────────────────────────────────────────────────
@bp.post("/api/sync/push/<table>")
def sync_push(table: str):
    err = _check_key()
    if err:
        return err
    if table not in SYNC_TABLES:
        return jsonify(error="Table non autorisée : %s" % table), 400
    data = request.get_json(force=True) or {}
    store_id = int(data.get("store_id", 0))
    session_id = int(data.get("session_id", 0))
    rows = data.get("rows") or []
    if not store_id or not rows:
        return jsonify(accepted=0)
    con = _db()
    n = upsert_batch(con, table, store_id, rows)
    con.commit()
    if session_id:
        add_rows_pushed(con, session_id, n)
        con.commit()
    return jsonify(accepted=n)


# ── Sync : fin de session ─────────────────────────────────────────────────────
@bp.post("/api/sync/finish")
def sync_finish():
    err = _check_key()
    if err:
        return err
    data = request.get_json(force=True) or {}
    session_id = int(data.get("session_id", 0))
    rows = int(data.get("rows", 0))
    status = data.get("status", "ok")
    error_msg = data.get("error_msg")
    if session_id:
        finish_sync(_db(), session_id, rows, status, error_msg)
    return jsonify(ok=True)


# ── Pending ops ───────────────────────────────────────────────────────────────
@bp.get("/api/pending_ops")
def list_pending_ops():
    err = _check_key()
    if err:
        return err
    store_id = int(request.args.get("store_id", 0))
    if not store_id:
        return jsonify(error="store_id manquant"), 400
    ops = pending_ops_for(_db(), store_id)
    return jsonify(ops=ops)


@bp.post("/api/pending_ops/<int:op_id>/<action>")
def report_op(op_id: int, action: str):
    err = _check_key()
    if err:
        return err
    if action not in ("done", "failed"):
        return jsonify(error="Action inconnue"), 400
    data = request.get_json(force=True) or {}
    error_msg = data.get("error_msg")
    status = "applied" if action == "done" else "failed"
    mark_op(_db(), op_id, status, error_msg)
    # Déclenche la notification asynchrone (si notifier configuré)
    notifier = current_app.config.get("notifier")
    if notifier:
        try:
            notifier.notify_pending(con=_db())
        except Exception:  # noqa: BLE001
            pass
    return jsonify(ok=True)


# ── Enqueue op (depuis l'appli bureau) ───────────────────────────────────────
@bp.post("/api/pending_ops/enqueue")
def enqueue():
    err = _check_key()
    if err:
        return err
    data = request.get_json(force=True) or {}
    store_id = int(data.get("store_id", 0))
    op_type = data.get("op_type", "")
    payload = data.get("payload") or {}
    if not store_id or not op_type:
        return jsonify(error="store_id et op_type requis"), 400
    op_id = enqueue_op(_db(), store_id, op_type, payload)
    return jsonify(op_id=op_id)


# ── Données pour les clients bureau (lecture via HTTP) ────────────────────────
@bp.get("/api/data/stores")
def data_stores():
    err = _check_key()
    if err:
        return err
    return jsonify(rows=store_status(_db()))


@bp.get("/api/data/tresorerie")
def data_tresorerie():
    err = _check_key()
    if err:
        return err
    day = request.args.get("day") or None
    return jsonify(rows=tresorerie_today(_db(), day))


@bp.get("/api/data/stock")
def data_stock():
    err = _check_key()
    if err:
        return err
    return jsonify(rows=stock_rows(_db(), request.args.get("q", "")))


@bp.get("/api/data/ventes")
def data_ventes():
    err = _check_key()
    if err:
        return err
    return jsonify(rows=ventes_rows(_db()))


@bp.get("/api/data/sync_logs")
def data_sync_logs():
    err = _check_key()
    if err:
        return err
    return jsonify(rows=sync_logs(_db()))


@bp.get("/api/data/pending_ops_recent")
def data_pending_ops_recent():
    err = _check_key()
    if err:
        return err
    return jsonify(rows=pending_ops_recent(_db()))


@bp.get("/api/data/article_search")
def data_article_search():
    err = _check_key()
    if err:
        return err
    return jsonify(rows=article_search(_db(), request.args.get("q", "")))


# ── Soumission d'une opération d'écriture (BDR / prix) ───────────────────────
#  Le client bureau n'écrit JAMAIS directement dans Firebird : il envoie l'op
#  ici. Le hub (magasin 1, toujours allumé, possède fbclient + accès Tailscale)
#  l'applique immédiatement si le magasin cible est joignable, sinon la met en
#  file (le magasin l'appliquera à son prochain démarrage, puis notification).
@bp.post("/api/op")
def submit_op():
    err = _check_key()
    if err:
        return err
    data = request.get_json(force=True) or {}
    store_id = int(data.get("store_id", 0))
    op_type = data.get("op_type", "")
    payload = data.get("payload") or {}
    if not store_id or op_type not in ("bdr_import", "price_update"):
        return jsonify(error="store_id et op_type valides requis"), 400

    registry = current_app.config.get("registry")
    store = None
    if registry:
        try:
            store = registry.get(store_id)
        except Exception:  # noqa: BLE001
            store = None

    online = bool(store and is_reachable(store.host, store.port))
    if online:
        try:
            kw = store.connect_kwargs()
            if op_type == "bdr_import":
                write_bdr(kw, payload.get("config") or {}, payload.get("lines") or [])
                n = len(payload.get("lines") or [])
            else:
                write_prices(kw, payload.get("changes") or [])
                n = len(payload.get("changes") or [])
            return jsonify(status="applied", count=n)
        except WriteError as exc:
            return jsonify(status="error", error=str(exc))
        except Exception as exc:  # noqa: BLE001
            return jsonify(status="error", error=str(exc))

    # Magasin hors ligne → file d'attente
    op_id = enqueue_op(_db(), store_id, op_type, payload)
    return jsonify(status="queued", op_id=op_id)
