"""Logique partagée « appliquer maintenant si en ligne, sinon mettre en file ».

Utilisée par /api/op (client bureau, JSON) ET par les pages mobiles (formulaires
serveur, même processus Flask — appel direct, sans aller-retour HTTP).
"""

from __future__ import annotations

import sqlite3

from hub.central_db import (
    enqueue_op, apply_barcode_ops_local, apply_price_changes_local,
    apply_item_edit_local, apply_item_add_local, apply_cancel_piece_local,
    completed_op_result, record_completed_op, log_immediate_op,
)
from hub.write_back import (
    is_reachable, write_bdr, write_prices, write_barcode_ops, write_item_edit,
    write_items_added, write_cancel_piece, WriteError,
)

OP_TYPES = ("bdr_import", "price_update", "barcode_ops", "item_edit", "item_add",
           "cancel_piece")


def submit_op(con: sqlite3.Connection, registry, store_id: int,
             op_type: str, payload: dict, op_uid: str | None = None) -> dict:
    """Applique l'opération sur le magasin s'il est joignable, sinon la met en
    file. Renvoie {"status": "applied"|"queued"|"error", ...}.

    `op_uid` (optionnel) rend l'appel idempotent : un retry client (timeout
    réseau après un import BDR long) renvoie le résultat déjà enregistré au
    lieu de ré-exécuter l'écriture — pas de double stock."""
    if op_type not in OP_TYPES:
        return {"status": "error", "error": "Type d'opération inconnu : %s" % op_type}

    if op_uid:
        prev = completed_op_result(con, op_uid)
        if prev is not None:
            return prev

    store = None
    if registry:
        try:
            store = registry.get(store_id)
        except Exception:  # noqa: BLE001
            # Le magasin n'existe pas dans stores.json (id erroné/périmé) : ce
            # n'est PAS "hors ligne", il ne faut donc pas mettre en file (l'op
            # ne serait jamais récupérée par aucun agent et resterait bloquée
            # indéfiniment en 'pending' sans jamais échouer ni notifier).
            return {"status": "error", "error": "Magasin inconnu (id=%s)." % store_id}

    online = bool(store and is_reachable(store.host, store.port))
    if online:
        try:
            kw = store.connect_kwargs()
            extra: dict = {}
            if op_type == "bdr_import":
                write_bdr(kw, payload.get("config") or {}, payload.get("lines") or [],
                         date_piece=payload.get("date_piece"))
                n = len(payload.get("lines") or [])
            elif op_type == "price_update":
                changes = payload.get("changes") or []
                write_prices(kw, changes)
                # Rafraîchir le miroir tout de suite : l'écriture directe ne
                # bumpe pas ARTICLE.DATEMODIF, la synchro incrémentale ne
                # rattraperait donc jamais ces nouvelles valeurs.
                apply_price_changes_local(con, store_id, changes)
                n = len(changes)
            elif op_type == "barcode_ops":
                bops = payload.get("ops") or []
                write_barcode_ops(kw, bops)
                apply_barcode_ops_local(con, store_id, bops)
                n = len(bops)
            elif op_type == "item_edit":
                edits = payload.get("edits") or []
                write_item_edit(kw, edits)
                apply_item_edit_local(con, store_id, edits)
                n = len(edits)
            elif op_type == "item_add":
                nopiece = payload.get("nopiece")
                lines = payload.get("lines") or []
                imp_result = write_items_added(kw, payload.get("config") or {}, nopiece, lines,
                                               date_piece=payload.get("date_piece"))
                apply_item_add_local(con, store_id, imp_result)
                n = len(lines)
                extra = {"nopiece": imp_result["nopiece"], "items": imp_result["items"]}
            else:  # cancel_piece
                nopiece = payload.get("nopiece")
                write_cancel_piece(kw, nopiece)
                apply_cancel_piece_local(con, store_id, nopiece)
                n = 1
            result = {"status": "applied", "count": n, **extra}
            if op_uid:
                record_completed_op(con, op_uid, result)
            log_immediate_op(con, store_id, op_type, payload, "applied", op_uid=op_uid)
            return result
        except WriteError as exc:
            log_immediate_op(con, store_id, op_type, payload, "failed",
                             error_msg=str(exc), op_uid=op_uid)
            return {"status": "error", "error": str(exc)}
        except Exception as exc:  # noqa: BLE001
            log_immediate_op(con, store_id, op_type, payload, "failed",
                             error_msg=str(exc), op_uid=op_uid)
            return {"status": "error", "error": str(exc)}

    op_id = enqueue_op(con, store_id, op_type, payload, op_uid=op_uid)
    return {"status": "queued", "op_id": op_id}
