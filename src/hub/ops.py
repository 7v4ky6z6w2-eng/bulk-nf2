"""Logique partagée « appliquer maintenant si en ligne, sinon mettre en file ».

Utilisée par /api/op (client bureau, JSON) ET par les pages mobiles (formulaires
serveur, même processus Flask — appel direct, sans aller-retour HTTP).
"""

from __future__ import annotations

import sqlite3

from hub.central_db import enqueue_op, apply_barcode_ops_local
from hub.write_back import is_reachable, write_bdr, write_prices, write_barcode_ops, WriteError

OP_TYPES = ("bdr_import", "price_update", "barcode_ops")


def submit_op(con: sqlite3.Connection, registry, store_id: int,
             op_type: str, payload: dict) -> dict:
    """Applique l'opération sur le magasin s'il est joignable, sinon la met en
    file. Renvoie {"status": "applied"|"queued"|"error", ...}."""
    if op_type not in OP_TYPES:
        return {"status": "error", "error": "Type d'opération inconnu : %s" % op_type}

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
            elif op_type == "price_update":
                write_prices(kw, payload.get("changes") or [])
                n = len(payload.get("changes") or [])
            else:  # barcode_ops
                bops = payload.get("ops") or []
                write_barcode_ops(kw, bops)
                apply_barcode_ops_local(con, store_id, bops)
                n = len(bops)
            return {"status": "applied", "count": n}
        except WriteError as exc:
            return {"status": "error", "error": str(exc)}
        except Exception as exc:  # noqa: BLE001
            return {"status": "error", "error": str(exc)}

    op_id = enqueue_op(con, store_id, op_type, payload)
    return {"status": "queued", "op_id": op_id}
