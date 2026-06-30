"""Accès à la base centrale SQLite (central.db) sur le poste hub.

Contient :
  * la création / migration du schéma (schema.sql) ;
  * ``upsert_batch`` générique avec liste blanche de tables et de colonnes
    (sécurité : aucun nom de table/colonne ne vient du réseau sans validation) ;
  * les helpers de file d'attente ``pending_ops`` ;
  * quelques lectures agrégées pour le tableau de bord.

Mode WAL activé : le tableau de bord peut lire pendant que l'agent écrit.
"""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timezone

SCHEMA_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "schema.sql")

# Colonnes acceptées par table (hors store_id / synced_at, injectées par l'upsert).
# Sert de liste blanche : une table absente d'ici est rejetée, une clé inconnue
# d'une ligne est ignorée.
COLUMN_MAP = {
    "article": ["ref_art", "designation", "code_barres", "prixventeht",
                "prixventettc", "prixachatht", "ctrlstock", "qtemin", "qtemax",
                "codefamille", "datemodif"],
    "famille": ["code_fam", "designation"],
    "tiers": ["code_tiers", "raison_sociale", "datemodif"],
    "depot": ["code_depot", "designation"],
    "type_piece": ["code_type_piece", "designation"],
    "mode_regl": ["code_mode_regl", "designation"],
    "piece": ["nopiece", "datepiece", "code_type_piece", "code_tiers",
              "code_depot", "montantht", "montantttc", "montantverse",
              "code_mode_regl", "annulee"],
    "item": ["nopiece", "noitem", "ref_art", "qte", "prixht", "remise",
             "marge", "annulee"],
    "stock_snapshot": ["ref_art", "code_depot", "qte_stock", "pump"],
    "tresorerie_snapshot": ["snap_date", "mode_paiement", "total_encaisse",
                            "nb_transactions"],
}

SYNC_TABLES = set(COLUMN_MAP)


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def connect(db_path: str) -> sqlite3.Connection:
    con = sqlite3.connect(db_path, timeout=30)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA busy_timeout=30000")
    con.row_factory = sqlite3.Row
    return con


def init_db(db_path: str) -> None:
    """Crée la base et le schéma s'ils n'existent pas (idempotent)."""
    os.makedirs(os.path.dirname(os.path.abspath(db_path)) or ".", exist_ok=True)
    with open(SCHEMA_PATH, "r", encoding="utf-8") as fh:
        schema = fh.read()
    con = connect(db_path)
    try:
        con.executescript(schema)
        con.commit()
    finally:
        con.close()


# --------------------------------------------------------------------------- #
#  Upsert générique
# --------------------------------------------------------------------------- #
def upsert_batch(con: sqlite3.Connection, table: str, store_id: int,
                 rows: list, synced_at: str | None = None) -> int:
    """Insère/remplace un lot de lignes pour une table miroir.

    `table` doit être dans la liste blanche. Chaque ligne est un dict dont seules
    les clés connues sont retenues. store_id et synced_at sont injectés.
    Renvoie le nombre de lignes traitées.
    """
    if table not in COLUMN_MAP:
        raise ValueError("Table non autorisée : %s" % table)
    if not rows:
        return 0
    synced_at = synced_at or now_iso()
    cols = COLUMN_MAP[table]
    all_cols = ["store_id"] + cols + ["synced_at"]
    placeholders = ",".join("?" * len(all_cols))
    sql = "INSERT OR REPLACE INTO %s (%s) VALUES (%s)" % (
        table, ",".join(all_cols), placeholders)
    params = []
    for row in rows:
        vals = [store_id] + [row.get(c) for c in cols] + [synced_at]
        params.append(vals)
    con.executemany(sql, params)
    return len(params)


# --------------------------------------------------------------------------- #
#  Sessions de synchro / présence des magasins
# --------------------------------------------------------------------------- #
def start_sync(con: sqlite3.Connection, store_id: int, store_name: str = "") -> int:
    cur = con.execute(
        "INSERT INTO sync_log (store_id, started, status) VALUES (?, ?, 'running')",
        (store_id, now_iso()))
    con.execute(
        "INSERT INTO store_meta (store_id, store_name, last_seen) VALUES (?, ?, ?) "
        "ON CONFLICT(store_id) DO UPDATE SET last_seen=excluded.last_seen, "
        "store_name=COALESCE(NULLIF(excluded.store_name,''), store_meta.store_name)",
        (store_id, store_name, now_iso()))
    con.commit()
    return cur.lastrowid


def finish_sync(con: sqlite3.Connection, session_id: int, rows: int,
                status: str = "ok", error_msg: str | None = None) -> None:
    con.execute(
        "UPDATE sync_log SET finished=?, rows_pushed=?, status=?, error_msg=? WHERE id=?",
        (now_iso(), rows, status, error_msg, session_id))
    con.commit()


def add_rows_pushed(con: sqlite3.Connection, session_id: int, n: int) -> None:
    con.execute("UPDATE sync_log SET rows_pushed = rows_pushed + ? WHERE id=?",
                (n, session_id))


# --------------------------------------------------------------------------- #
#  File d'attente des écritures (pending_ops)
# --------------------------------------------------------------------------- #
def enqueue_op(con: sqlite3.Connection, store_id: int, op_type: str,
               payload: dict) -> int:
    cur = con.execute(
        "INSERT INTO pending_ops (store_id, op_type, payload, created_at, status) "
        "VALUES (?, ?, ?, ?, 'pending')",
        (store_id, op_type, json.dumps(payload, ensure_ascii=False), now_iso()))
    con.commit()
    return cur.lastrowid


def pending_ops_for(con: sqlite3.Connection, store_id: int) -> list:
    rows = con.execute(
        "SELECT id, op_type, payload FROM pending_ops "
        "WHERE store_id=? AND status='pending' ORDER BY id", (store_id,)).fetchall()
    return [{"id": r["id"], "op_type": r["op_type"],
             "payload": json.loads(r["payload"])} for r in rows]


def mark_op(con: sqlite3.Connection, op_id: int, status: str,
            error_msg: str | None = None) -> None:
    con.execute(
        "UPDATE pending_ops SET status=?, applied_at=?, error_msg=? WHERE id=?",
        (status, now_iso(), error_msg, op_id))
    con.commit()


def ops_to_notify(con: sqlite3.Connection) -> list:
    """Opérations terminées (applied/failed) pas encore notifiées."""
    rows = con.execute(
        "SELECT id, store_id, op_type, status, error_msg, payload FROM pending_ops "
        "WHERE status IN ('applied','failed') AND notified=0 ORDER BY id").fetchall()
    return [dict(r) for r in rows]


def mark_notified(con: sqlite3.Connection, op_id: int) -> None:
    con.execute("UPDATE pending_ops SET notified=1 WHERE id=?", (op_id,))
    con.commit()


# --------------------------------------------------------------------------- #
#  Lectures pour le tableau de bord
# --------------------------------------------------------------------------- #
def store_status(con: sqlite3.Connection) -> list:
    """Pour chaque magasin connu : nom + dernière synchro réussie."""
    rows = con.execute(
        "SELECT sm.store_id, sm.store_name, sm.last_seen, "
        "  (SELECT MAX(finished) FROM sync_log s "
        "    WHERE s.store_id=sm.store_id AND s.status='ok') AS last_ok "
        "FROM store_meta sm ORDER BY sm.store_id").fetchall()
    return [dict(r) for r in rows]


def tresorerie_today(con: sqlite3.Connection, day: str | None = None) -> list:
    day = day or datetime.now().strftime("%Y-%m-%d")
    rows = con.execute(
        "SELECT store_id, mode_paiement, total_encaisse, nb_transactions, synced_at "
        "FROM tresorerie_snapshot WHERE snap_date=? ORDER BY store_id, mode_paiement",
        (day,)).fetchall()
    return [dict(r) for r in rows]


def stock_rows(con: sqlite3.Connection, search: str = "", limit: int = 500) -> list:
    q = "%" + (search or "") + "%"
    rows = con.execute(
        "SELECT a.store_id, a.ref_art, a.designation, s.code_depot, s.qte_stock "
        "FROM stock_snapshot s JOIN article a "
        "  ON s.store_id=a.store_id AND s.ref_art=a.ref_art "
        "WHERE a.ref_art LIKE ? OR a.designation LIKE ? "
        "ORDER BY a.store_id, a.ref_art LIMIT ?", (q, q, limit)).fetchall()
    return [dict(r) for r in rows]


def ventes_rows(con: sqlite3.Connection, days: int = 7, limit: int = 300) -> list:
    rows = con.execute(
        "SELECT p.store_id, p.datepiece, p.nopiece, "
        "  COALESCE(t.raison_sociale, p.code_tiers) AS client, "
        "  p.montantttc, p.code_mode_regl "
        "FROM piece p LEFT JOIN tiers t "
        "  ON p.store_id=t.store_id AND p.code_tiers=t.code_tiers "
        "WHERE p.datepiece >= date('now', ?) "
        "ORDER BY p.datepiece DESC LIMIT ?", ("-%d days" % days, limit)).fetchall()
    return [dict(r) for r in rows]


def sync_logs(con: sqlite3.Connection, limit: int = 50) -> list:
    rows = con.execute(
        "SELECT l.id, l.store_id, m.store_name, l.started, l.finished, "
        "  l.rows_pushed, l.status, l.error_msg "
        "FROM sync_log l LEFT JOIN store_meta m ON l.store_id=m.store_id "
        "ORDER BY l.id DESC LIMIT ?", (limit,)).fetchall()
    return [dict(r) for r in rows]


def pending_ops_recent(con: sqlite3.Connection, limit: int = 50) -> list:
    rows = con.execute(
        "SELECT id, store_id, op_type, created_at, status, error_msg "
        "FROM pending_ops ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    return [dict(r) for r in rows]


def article_search(con: sqlite3.Connection, query: str = "", limit: int = 200) -> list:
    """Articles correspondant à la recherche, une ligne par (article, magasin)."""
    q = "%" + (query or "") + "%"
    rows = con.execute(
        "SELECT ref_art, designation, store_id, prixventeht "
        "FROM article WHERE ref_art LIKE ? OR designation LIKE ? "
        "ORDER BY ref_art, store_id LIMIT ?", (q, q, limit)).fetchall()
    return [dict(r) for r in rows]
