"""Endpoints Flask du hub : réception des données agents + pending_ops."""

from __future__ import annotations

import json
import sqlite3

from flask import Blueprint, jsonify, request, current_app

from hub.central_db import (
    upsert_batch, start_sync, finish_sync, add_rows_pushed,
    pending_ops_for, mark_op, enqueue_op, SYNC_TABLES,
    store_status, tresorerie_today, tresorerie_caisses, stock_rows, ventes_rows,
    sync_logs, pending_ops_recent, article_search,
    tresorerie_days, tresorerie_range, stock_search,
)
from hub.central_db import article_barcodes, replace_snapshot, \
    apply_price_changes_local, name_match_suggestions, confirm_article_link, \
    price_sync_candidates, ArticleLinkConflict
from hub.ops import submit_op as _submit_op, OP_TYPES

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
    # Tables « instantané » : le 1er lot du cycle porte replace=true → on purge
    # l'état précédent du magasin pour que les lignes disparues côté Firebird
    # (code-barres supprimé, stock à zéro…) disparaissent aussi du miroir.
    # Purge + upsert dans la même transaction (commit unique ci-dessous).
    if data.get("replace"):
        replace_snapshot(con, table, store_id, rows)
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
    con = _db()
    mark_op(con, op_id, status, error_msg)
    # Op de prix appliquée par l'agent (magasin qui était hors ligne) :
    # répercuter aussi sur le miroir central, comme pour une écriture directe
    # (l'écriture Firebird de l'agent ne bumpe pas ARTICLE.DATEMODIF).
    if status == "applied":
        row = con.execute("SELECT store_id, op_type, payload FROM pending_ops "
                          "WHERE id=?", (op_id,)).fetchone()
        if row and row["op_type"] == "price_update":
            try:
                payload = json.loads(row["payload"] or "{}")
                apply_price_changes_local(con, row["store_id"],
                                          payload.get("changes") or [])
            except (ValueError, TypeError):
                pass
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
    caisse = request.args.get("caisse") or None
    return jsonify(rows=tresorerie_today(_db(), day, caisse))


@bp.get("/api/data/tresorerie_days")
def data_tresorerie_days():
    err = _check_key()
    if err:
        return err
    return jsonify(days=tresorerie_days(_db()))


@bp.get("/api/data/tresorerie_range")
def data_tresorerie_range():
    err = _check_key()
    if err:
        return err
    date_from = request.args.get("from", "")
    date_to = request.args.get("to", "")
    if not date_from or not date_to:
        return jsonify(error="from/to manquants"), 400
    caisse = request.args.get("caisse") or None
    store_id_arg = request.args.get("store_id")
    store_id = int(store_id_arg) if store_id_arg else None
    return jsonify(rows=tresorerie_range(_db(), date_from, date_to, caisse, store_id))


@bp.get("/api/data/caisses")
def data_caisses():
    err = _check_key()
    if err:
        return err
    return jsonify(rows=tresorerie_caisses(_db()))


@bp.get("/api/data/stock_search")
def data_stock_search():
    err = _check_key()
    if err:
        return err
    return jsonify(rows=stock_search(_db(), request.args.get("q", "")))


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


@bp.get("/api/data/article_barcodes")
def data_article_barcodes():
    err = _check_key()
    if err:
        return err
    ref = request.args.get("ref", "")
    if not ref:
        return jsonify(rows=[])
    return jsonify(rows=article_barcodes(_db(), ref))


@bp.get("/api/data/name_match_suggestions")
def data_name_match_suggestions():
    err = _check_key()
    if err:
        return err
    return jsonify(groups=name_match_suggestions(_db()))


@bp.post("/api/data/confirm_link")
def data_confirm_link():
    err = _check_key()
    if err:
        return err
    data = request.get_json(force=True) or {}
    try:
        link_key = confirm_article_link(_db(), data.get("members") or [])
    except ArticleLinkConflict as exc:
        return jsonify(error=str(exc)), 400
    return jsonify(ok=True, link_key=link_key)


@bp.get("/api/data/price_sync_candidates")
def data_price_sync_candidates():
    err = _check_key()
    if err:
        return err
    source = int(request.args.get("source_store_id", 0))
    if not source:
        return jsonify(error="source_store_id manquant"), 400
    return jsonify(groups=price_sync_candidates(_db(), source))


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
    op_uid = data.get("op_uid") or None
    if not store_id or op_type not in OP_TYPES:
        return jsonify(error="store_id et op_type valides requis"), 400

    registry = current_app.config.get("registry")
    result = _submit_op(_db(), registry, store_id, op_type, payload, op_uid=op_uid)
    return jsonify(**result)


# ── Synchro fournisseur (bon de livraison -> bon de réception) ───────────────
#  Appelé par l'exe qui tourne CHEZ le fournisseur : lignes brutes de son
#  Firebird, jamais de logique métier côté exe (tout le rapprochement se fait
#  ici, cf. hub/fournisseur.py — plus facile à corriger sans redéployer un
#  binaire sur un poste distant qu'on ne contrôle pas directement).
@bp.post("/api/fournisseur/lines")
def fournisseur_lines():
    err = _check_key()
    if err:
        return err
    from hub.fournisseur import process_batch
    data = request.get_json(force=True) or {}
    lines = data.get("lines") or []
    if not lines:
        return jsonify(error="Aucune ligne."), 400
    registry = current_app.config.get("registry")
    results = process_batch(_db(), registry, lines)
    return jsonify(results=results)


@bp.get("/api/fournisseur/mapping")
def fournisseur_mapping_get():
    """Mapping CODE_TIERS -> magasin actuellement enregistré — lu par l'exe
    fournisseur à l'ouverture de « Correspondance clients -> magasins » pour
    PRÉ-SÉLECTIONNER ce qui est déjà mappé (sinon la boîte de dialogue
    repartait de zéro à chaque ouverture : impossible de voir que quelque
    chose était déjà enregistré côté hub, ça donnait l'impression que rien
    n'était jamais sauvegardé). Le tableau de bord web reste la source de
    vérité / l'endroit où corriger le mapping ensuite ; ceci n'est qu'une
    lecture.

    Aussi le SEUL appel que run_cycle() (fournisseur_sync.py) fait à CHAQUE
    passage, sans condition, avant même de lire son Firebird local — donc le
    signal de battement de coeur le plus fiable pour savoir si l'outil est
    encore en vie (fournisseur_mark_seen), contrairement à /api/fournisseur/
    lines qui n'est appelé que s'il y a effectivement une ligne à envoyer."""
    err = _check_key()
    if err:
        return err
    from hub.central_db import fournisseur_mapping, fournisseur_mark_seen
    fournisseur_mark_seen(_db())
    return jsonify(mapping=fournisseur_mapping(_db()))


@bp.post("/api/fournisseur/mapping")
def fournisseur_mapping_bootstrap():
    """Premier lancement de l'exe fournisseur : il propose une liste de
    CODE_TIERS -> magasin (choisis par l'utilisateur dans son Firebird) : on
    les enregistre. Éditable ensuite sur le tableau de bord web
    (/fournisseur-mapping), qui reste la source de vérité."""
    err = _check_key()
    if err:
        return err
    from hub.central_db import fournisseur_set_mapping
    data = request.get_json(force=True) or {}
    entries = data.get("mapping") or []
    con = _db()
    n = 0
    for e in entries:
        code_tiers = (e.get("code_tiers") or "").strip()
        store_id = e.get("store_id")
        if not code_tiers or not store_id:
            continue
        fournisseur_set_mapping(con, code_tiers, int(store_id), e.get("raison_sociale"))
        n += 1
    return jsonify(ok=True, count=n)


@bp.post("/api/fournisseur/reconcile")
def fournisseur_reconcile():
    """Onglet « Vérification article » de l'exe fournisseur — diagnostic
    manuel en LECTURE SEULE (jamais appelé par la synchro automatique) : pour
    un article recherché, compare ce que le père a livré (father_lines, lues
    par l'exe sur son propre Firebird) à ce que chaque magasin destinataire a
    déjà reçu sur la même période, et signale si la référence du père
    correspond à une référence DIFFÉRENTE côté magasin."""
    err = _check_key()
    if err:
        return err
    from hub.fournisseur import reconcile_article
    data = request.get_json(force=True) or {}
    ref = (data.get("ref") or "").strip() or None
    designation = (data.get("designation") or "").strip() or None
    if not ref and not designation:
        return jsonify(error="Indiquez ref ou designation."), 400
    days = int(data.get("days") or 30)
    father_lines = data.get("father_lines") or []
    registry = current_app.config.get("registry")
    report = reconcile_article(_db(), registry, ref, designation, days, father_lines)
    return jsonify(report=report)
