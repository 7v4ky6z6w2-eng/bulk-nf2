"""Endpoints Flask du hub : réception des données agents + pending_ops."""

from __future__ import annotations

import json
import sqlite3

from flask import Blueprint, jsonify, request, current_app

from hub.central_db import (
    upsert_batch, start_sync, finish_sync, add_rows_pushed,
    pending_ops_for, mark_op, enqueue_op, SYNC_TABLES,
)

bp = Blueprint("api", __name__)


def _db() -> sqlite3.Connection:
    return current_app.config["db_connection"]()


def _check_key():
    key = current_app.config.get("API_KEY", "")
    if key and request.headers.get("X-Api-Key") != key:
        return jsonify(error="Clé API invalide"), 403
    return None


# ── Santé ─────────────────────────────────────────────────────────────────────
@bp.get("/api/ping")
def ping():
    return jsonify(ok=True)


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
