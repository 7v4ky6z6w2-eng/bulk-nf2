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
import uuid
from datetime import datetime, timezone

SCHEMA_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "schema.sql")

# Colonnes acceptées par table (hors store_id / synced_at, injectées par l'upsert).
# Sert de liste blanche : une table absente d'ici est rejetée, une clé inconnue
# d'une ligne est ignorée.
COLUMN_MAP = {
    "article": ["ref_art", "designation", "code_barres", "prixventeht",
                "prixventettc", "prixachatht", "ctrlstock", "qtemin", "qtemax",
                "codefamille", "datemodif",
                "prixttcpromo", "activepromo", "datedebpromo", "datefinpromo"],
    "famille": ["code_fam", "designation"],
    "tiers": ["code_tiers", "raison_sociale", "datemodif"],
    "depot": ["code_depot", "designation"],
    "type_piece": ["code_type_piece", "designation"],
    "mode_regl": ["code_mode_regl", "designation"],
    "piece": ["nopiece", "datepiece", "code_type_piece", "code_tiers",
              "code_depot", "montantht", "montantttc", "montantverse",
              "code_mode_regl", "annulee", "refdoc"],
    "item": ["nopiece", "noitem", "ref_art", "qte", "prixht", "remise",
             "marge", "annulee"],
    "stock_snapshot": ["ref_art", "code_depot", "qte_stock", "pump"],
    "equiv_cbarres": ["ref_art", "code_barres"],
    "tresorerie_snapshot": ["snap_date", "caisse", "sens", "mode_paiement",
                            "total_encaisse", "nb_transactions"],
}

SYNC_TABLES = set(COLUMN_MAP)

# Types de PIECE qui comptent comme une VENTE — liste explicite (allowlist),
# confirmée par l'utilisateur sur données réelles via /diagnostic-types : la
# table PIECE mélange des dizaines de types (bons de réception, corrections de
# stock, transferts de caisse, proformas, audits...) et seuls ces deux-là
# représentent une vente effective. Un allowlist plutôt qu'un denylist : un
# type de pièce encore inconnu (nouvelle version de Netfact2/PRIME, variante
# d'installation) est exclu par défaut au lieu d'être compté par erreur tant
# que personne ne l'a repéré.
#   PC_VE_B    Bon de livraison (vente)
#   PC_VE_TIK  Ticket point de vente
VENTE_TYPES = ("PC_VE_B", "PC_VE_TIK")
_VENTE_SQL = "(" + ",".join("'%s'" % t for t in VENTE_TYPES) + ")"


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
        # Migrations D'ABORD : schema.sql référence des colonnes récentes (ex.
        # l'index partiel sur pending_ops.op_uid) qui doivent exister avant
        # executescript sur une base ancienne. Sur une base neuve, _migrate ne
        # fait rien (tables absentes).
        _migrate(con)
        con.commit()
        con.executescript(schema)
        con.commit()
    finally:
        con.close()


def _migrate(con: sqlite3.Connection) -> None:
    """Migrations légères des bases déjà créées avant une évolution de schéma."""
    # Ajout des colonnes « caisse » et « sens » à tresorerie_snapshot. La
    # contrainte UNIQUE doit les inclure, donc on recrée la table — mais en
    # PRÉSERVANT les lignes existantes (copiées avec caisse/sens par défaut),
    # au lieu de les jeter : un simple redémarrage du hub ne doit jamais
    # effacer une journée de trésorerie déjà synchronisée.
    cols = [r[1] for r in con.execute("PRAGMA table_info(tresorerie_snapshot)").fetchall()]
    if cols and ("caisse" not in cols or "sens" not in cols):
        con.execute("ALTER TABLE tresorerie_snapshot RENAME TO tresorerie_snapshot_old")
        con.executescript(
            "CREATE TABLE tresorerie_snapshot ("
            " id INTEGER PRIMARY KEY AUTOINCREMENT, store_id INTEGER NOT NULL,"
            " snap_date TEXT NOT NULL, caisse TEXT DEFAULT '(globale)',"
            " sens TEXT DEFAULT 'entree', mode_paiement TEXT NOT NULL,"
            " total_encaisse REAL DEFAULT 0, nb_transactions INTEGER DEFAULT 0,"
            " synced_at TEXT NOT NULL,"
            " UNIQUE (store_id, snap_date, caisse, sens, mode_paiement));"
            "CREATE INDEX IF NOT EXISTS idx_treso_store_date "
            " ON tresorerie_snapshot (store_id, snap_date);")
        caisse_expr = "caisse" if "caisse" in cols else "'(globale)'"
        sens_expr = "sens" if "sens" in cols else "'entree'"
        con.execute(
            "INSERT OR IGNORE INTO tresorerie_snapshot "
            " (store_id, snap_date, caisse, sens, mode_paiement, total_encaisse, "
            "  nb_transactions, synced_at) "
            "SELECT store_id, snap_date, %s, %s, mode_paiement, total_encaisse, "
            "  nb_transactions, synced_at FROM tresorerie_snapshot_old"
            % (caisse_expr, sens_expr))
        con.execute("DROP TABLE tresorerie_snapshot_old")

    # Colonne op_uid (idempotence) sur pending_ops (bases créées avant).
    op_cols = [r[1] for r in con.execute("PRAGMA table_info(pending_ops)").fetchall()]
    if op_cols and "op_uid" not in op_cols:
        con.execute("ALTER TABLE pending_ops ADD COLUMN op_uid TEXT")
        con.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_ops_uid "
                    "ON pending_ops (op_uid) WHERE op_uid IS NOT NULL")

    # Colonnes promo sur article (bases créées avant l'édition des promos).
    art_cols = [r[1] for r in con.execute("PRAGMA table_info(article)").fetchall()]
    if art_cols:
        for col, decl in (("prixttcpromo", "REAL"), ("activepromo", "INTEGER"),
                          ("datedebpromo", "TEXT"), ("datefinpromo", "TEXT")):
            if col not in art_cols:
                con.execute("ALTER TABLE article ADD COLUMN %s %s" % (col, decl))

    # Colonne tva sur fournisseur_pending (bases créées avant son ajout) : sans
    # elle, une ligne mise en attente (rapprochement par nom) perdait son taux
    # de TVA réel et retombait sur le défaut (19 %) une fois validée depuis le
    # tableau de bord — cf. fournisseur.apply_new_line / _build_bdr_line.
    fp_cols = [r[1] for r in con.execute("PRAGMA table_info(fournisseur_pending)").fetchall()]
    if fp_cols and "tva" not in fp_cols:
        con.execute("ALTER TABLE fournisseur_pending ADD COLUMN tva REAL")

    # Colonne refdoc sur piece (bases créées avant son ajout) : sert à
    # retrouver, via le miroir, une réception créée pour un BL fournisseur
    # sans jamais être repassé par fournisseur_sync_state (créée hors ligne,
    # appliquée par l'agent ensuite — voir hub.fournisseur pour le repli).
    piece_cols = [r[1] for r in con.execute("PRAGMA table_info(piece)").fetchall()]
    if piece_cols and "refdoc" not in piece_cols:
        con.execute("ALTER TABLE piece ADD COLUMN refdoc TEXT")


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
    try:
        con.executemany(sql, params)
        return len(params)
    except sqlite3.Error:
        # Une seule ligne malformée (ex. clé métier NULL) fait échouer
        # executemany() et annule TOUT le lot (rollback implicite). On repasse
        # ligne par ligne pour ne perdre que la ligne fautive, sinon l'agent
        # ré-envoie indéfiniment le même lot en butant toujours sur la même
        # ligne (la synchro du magasin resterait bloquée).
        n = 0
        for p in params:
            try:
                con.execute(sql, p)
                n += 1
            except sqlite3.Error:
                continue
        return n


# Tables « instantané » : l'agent pousse à chaque cycle l'état COMPLET actuel.
# Sans purge préalable, une ligne disparue côté Firebird (code-barres supprimé,
# stock retombé…) resterait indéfiniment dans le miroir.
SNAPSHOT_TABLES = {"stock_snapshot", "equiv_cbarres", "tresorerie_snapshot"}


def replace_snapshot(con: sqlite3.Connection, table: str, store_id: int,
                     rows: list) -> None:
    """Purge le miroir du magasin avant l'upsert d'un lot « replace ».

    Pour tresorerie_snapshot on ne purge que les journées présentes dans le
    lot (l'historique des jours précédents est conservé). Appelé uniquement
    avec un lot NON vide (un agent qui n'a rien lu ne vide jamais le miroir).
    """
    if table not in SNAPSHOT_TABLES or not rows:
        return
    if table == "tresorerie_snapshot":
        days = sorted({r.get("snap_date") for r in rows if r.get("snap_date")})
        if not days:
            return
        marks = ",".join("?" * len(days))
        con.execute("DELETE FROM tresorerie_snapshot WHERE store_id=? "
                    "AND snap_date IN (%s)" % marks, [store_id] + days)
    else:
        con.execute("DELETE FROM %s WHERE store_id=?" % table, (store_id,))


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
               payload: dict, op_uid: str | None = None) -> int:
    if op_uid:
        # Idempotence : le même uid re-soumis (retry client) ne crée pas de
        # doublon, on renvoie l'op déjà en file.
        row = con.execute("SELECT id FROM pending_ops WHERE op_uid=?",
                          (op_uid,)).fetchone()
        if row:
            return row["id"]
    cur = con.execute(
        "INSERT INTO pending_ops (store_id, op_type, payload, created_at, status, op_uid) "
        "VALUES (?, ?, ?, ?, 'pending', ?)",
        (store_id, op_type, json.dumps(payload, ensure_ascii=False), now_iso(), op_uid))
    con.commit()
    return cur.lastrowid


def log_immediate_op(con: sqlite3.Connection, store_id: int, op_type: str,
                     payload: dict, status: str, error_msg: str | None = None,
                     op_uid: str | None = None) -> int:
    """Trace une op appliquée TOUT DE SUITE (magasin en ligne) dans pending_ops,
    pour qu'elle apparaisse dans l'historique au même titre que les ops mises en
    file. notified=1 dès l'insertion : l'utilisateur est déjà devant l'écran qui
    vient de lui afficher le résultat, pas besoin d'un Telegram redondant.

    Upsert sur op_uid : un retry (ex. BDR ré-essayé après échec avec le même
    op_uid) met à jour la ligne existante au lieu de violer l'index unique
    idx_ops_uid (sinon IntegrityError sur le 2e essai)."""
    now = now_iso()
    cur = con.execute(
        "INSERT INTO pending_ops (store_id, op_type, payload, created_at, applied_at, "
        "                        status, error_msg, notified, op_uid) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?) "
        "ON CONFLICT(op_uid) WHERE op_uid IS NOT NULL DO UPDATE SET "
        "  applied_at=excluded.applied_at, status=excluded.status, "
        "  error_msg=excluded.error_msg, notified=1",
        (store_id, op_type, json.dumps(payload, ensure_ascii=False), now, now,
         status, error_msg, op_uid))
    con.commit()
    return cur.lastrowid


def ops_history(con: sqlite3.Connection, limit: int = 100) -> list:
    """Historique des opérations (prix / codes-barres / BDR), appliquées tout de
    suite ou mises en file, les plus récentes d'abord."""
    rows = con.execute(
        "SELECT id, store_id, op_type, payload, created_at, applied_at, status, "
        "       error_msg FROM pending_ops ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        try:
            payload = json.loads(d.pop("payload") or "{}")
        except (TypeError, ValueError):
            payload = {}
        if d["op_type"] == "price_update":
            changes = payload.get("changes") or []
            refs = ", ".join(str(c.get("ref0")) for c in changes[:3])
            if len(changes) > 3:
                refs += "…"
            d["detail"] = "%d article(s) : %s" % (len(changes), refs)
        elif d["op_type"] == "barcode_ops":
            ops = payload.get("ops") or []
            d["detail"] = "%d code(s)-barres" % len(ops)
        elif d["op_type"] == "bdr_import":
            lines = payload.get("lines") or []
            d["detail"] = "Bon : %d ligne(s)" % len(lines)
        else:
            d["detail"] = ""
        out.append(d)
    return out


def completed_op_result(con: sqlite3.Connection, op_uid: str) -> dict | None:
    """Résultat enregistré d'une op déjà appliquée directement (par op_uid)."""
    row = con.execute("SELECT result FROM completed_ops WHERE op_uid=?",
                      (op_uid,)).fetchone()
    return json.loads(row["result"]) if row else None


def record_completed_op(con: sqlite3.Connection, op_uid: str, result: dict) -> None:
    con.execute("INSERT OR REPLACE INTO completed_ops (op_uid, result, created_at) "
                "VALUES (?, ?, ?)",
                (op_uid, json.dumps(result, ensure_ascii=False), now_iso()))
    con.commit()


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


def digest_already_sent(con: sqlite3.Connection, digest_type: str, digest_date: str) -> bool:
    row = con.execute(
        "SELECT 1 FROM digest_log WHERE digest_type=? AND digest_date=?",
        (digest_type, digest_date)).fetchone()
    return row is not None


def mark_digest_sent(con: sqlite3.Connection, digest_type: str, digest_date: str) -> None:
    con.execute(
        "INSERT OR IGNORE INTO digest_log (digest_type, digest_date, sent_at) "
        "VALUES (?, ?, ?)", (digest_type, digest_date, now_iso()))
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


def tresorerie_today(con: sqlite3.Connection, day: str | None = None,
                     caisse: str | None = None) -> list:
    """Encaissements du jour. Si `caisse` est fourni (et != 'Globale'), on ne
    garde que cette caisse ; sinon toutes les caisses sont renvoyées (le client
    peut cumuler pour obtenir la vue « Globale »)."""
    day = day or datetime.now().strftime("%Y-%m-%d")
    sql = ("SELECT store_id, caisse, sens, mode_paiement, total_encaisse, "
           "nb_transactions, synced_at FROM tresorerie_snapshot WHERE snap_date=?")
    params = [day]
    if caisse and caisse.lower() not in ("globale", "(globale)", "toutes", ""):
        sql += " AND caisse=?"
        params.append(caisse)
    sql += " ORDER BY store_id, caisse, mode_paiement"
    return [dict(r) for r in con.execute(sql, params).fetchall()]


def tresorerie_caisses(con: sqlite3.Connection) -> list:
    """Liste des caisses connues (toutes dates), pour peupler le sélecteur."""
    rows = con.execute(
        "SELECT DISTINCT store_id, caisse FROM tresorerie_snapshot "
        "ORDER BY store_id, caisse").fetchall()
    return [dict(r) for r in rows]


def tresorerie_days(con: sqlite3.Connection) -> list:
    """Dates disponibles dans l'historique trésorerie, plus récentes d'abord —
    sert à peupler un sélecteur/calendrier de dates passées."""
    rows = con.execute(
        "SELECT DISTINCT snap_date FROM tresorerie_snapshot "
        "ORDER BY snap_date DESC").fetchall()
    return [r["snap_date"] for r in rows]


def tresorerie_range(con: sqlite3.Connection, date_from: str, date_to: str,
                     caisse: str | None = None, store_id: int | None = None) -> list:
    """Totaux entrée/sortie PAR JOUR sur une période [date_from, date_to]
    (bornes incluses) — l'historique complet, un jour = une ligne, magasins et
    modes de paiement cumulés (filtrable par caisse et/ou magasin). Sert à
    parcourir les dates passées et à obtenir le total sur N jours (somme des
    lignes renvoyées, faite côté client)."""
    sql = ("SELECT snap_date, sens, SUM(total_encaisse) AS total, "
           "SUM(nb_transactions) AS nb_transactions "
           "FROM tresorerie_snapshot WHERE snap_date BETWEEN ? AND ?")
    params: list = [date_from, date_to]
    if caisse and caisse.lower() not in ("globale", "(globale)", "toutes", ""):
        sql += " AND caisse=?"
        params.append(caisse)
    if store_id:
        sql += " AND store_id=?"
        params.append(store_id)
    sql += " GROUP BY snap_date, sens"
    rows = con.execute(sql, params).fetchall()

    by_day: dict = {}
    for r in rows:
        d = by_day.setdefault(r["snap_date"], {
            "snap_date": r["snap_date"], "entree": 0.0, "sortie": 0.0,
            "nb_transactions": 0})
        d["sortie" if r["sens"] == "sortie" else "entree"] += r["total"] or 0.0
        d["nb_transactions"] += r["nb_transactions"] or 0
    out = list(by_day.values())
    for d in out:
        d["solde"] = d["entree"] - d["sortie"]
    out.sort(key=lambda d: d["snap_date"])
    return out


def tresorerie_range_by_store(con: sqlite3.Connection, date_from: str, date_to: str,
                              caisse_by_store: dict | None = None) -> list:
    """Entrées/sorties PAR JOUR ET PAR MAGASIN sur une période — même source et
    même méthode que la Vue d'ensemble (tresorerie_snapshot, PAS la table
    PIECE) : entrées/sorties de caisse réelles, pas un total de pièces.

    `caisse_by_store` : {store_id: nom_de_caisse} pour restreindre UN magasin
    donné à UNE caisse précise (ex. un magasin où plusieurs caisses sont
    suivies mais une seule est fiable) ; un magasin absent du dict, ou une
    valeur vide/'Globale', cumule TOUTES ses caisses — comme le sélecteur
    « Globale (toutes) » de la page Trésorerie.

    Renvoie une liste triée par jour décroissant, un élément par jour :
    {"jour": ..., "par_magasin": {store_id: {"entree", "sortie", "nb"}}}."""
    caisse_by_store = caisse_by_store or {}
    rows = con.execute(
        "SELECT store_id, snap_date, caisse, sens, "
        "       SUM(total_encaisse) AS total, SUM(nb_transactions) AS nb "
        "FROM tresorerie_snapshot WHERE snap_date BETWEEN ? AND ? "
        "GROUP BY store_id, snap_date, caisse, sens",
        (date_from, date_to)).fetchall()

    by_day: dict = {}
    for r in rows:
        sid = r["store_id"]
        wanted = (caisse_by_store.get(sid) or "").strip()
        row_caisse = (r["caisse"] or "").strip()
        if wanted and wanted.lower() not in ("globale", "(globale)", "toutes") \
           and row_caisse != wanted:
            continue
        d = by_day.setdefault(r["snap_date"], {"jour": r["snap_date"], "par_magasin": {}})
        pm = d["par_magasin"].setdefault(sid, {"entree": 0.0, "sortie": 0.0, "nb": 0})
        val = r["total"] or 0.0
        if r["sens"] == "sortie":
            pm["sortie"] += val
        else:
            pm["entree"] += val
            pm["nb"] += r["nb"] or 0
    return sorted(by_day.values(), key=lambda d: d["jour"], reverse=True)


def stock_rows(con: sqlite3.Connection, search: str = "", limit: int = 500,
              offset: int = 0) -> list:
    q = "%" + (search or "") + "%"
    rows = con.execute(
        "SELECT a.store_id, a.ref_art, a.designation, s.code_depot, s.qte_stock "
        "FROM stock_snapshot s JOIN article a "
        "  ON s.store_id=a.store_id AND s.ref_art=a.ref_art "
        "WHERE a.ref_art LIKE ? OR a.designation LIKE ? "
        "ORDER BY a.store_id, a.ref_art LIMIT ? OFFSET ?",
        (q, q, limit, offset)).fetchall()
    return [dict(r) for r in rows]


def stock_count(con: sqlite3.Connection, search: str = "") -> int:
    q = "%" + (search or "") + "%"
    row = con.execute(
        "SELECT COUNT(*) AS n FROM stock_snapshot s JOIN article a "
        "  ON s.store_id=a.store_id AND s.ref_art=a.ref_art "
        "WHERE a.ref_art LIKE ? OR a.designation LIKE ?", (q, q)).fetchone()
    return row["n"] if row else 0


def stock_search(con: sqlite3.Connection, query: str = "", limit: int = 200) -> list:
    """Stock par PRODUIT et par MAGASIN — une ligne par (article, magasin),
    quantité cumulée sur tous les dépôts de ce magasin (jamais cumulée entre
    magasins : le but est justement de voir le détail par magasin, pas un
    total global). Porte aussi `match_key` (cf. _MATCH_KEY_SQL, même logique
    que article_search) pour que le client regroupe les lignes d'un même
    produit vendu sous des références différentes selon le magasin."""
    q = "%" + (query or "") + "%"
    rows = con.execute(
        "SELECT a.ref_art, a.designation, a.store_id, "
        "       " + _MATCH_KEY_SQL + ", "
        "       COALESCE(SUM(s.qte_stock), 0) AS qte_stock "
        "FROM article a LEFT JOIN stock_snapshot s "
        "  ON s.store_id=a.store_id AND s.ref_art=a.ref_art "
        "WHERE a.ref_art LIKE ? OR a.designation LIKE ? OR a.code_barres LIKE ? "
        "   OR EXISTS (SELECT 1 FROM equiv_cbarres e WHERE e.store_id=a.store_id "
        "              AND e.ref_art=a.ref_art AND e.code_barres LIKE ?) "
        "GROUP BY a.store_id, a.ref_art "
        "ORDER BY a.ref_art, a.store_id LIMIT ?", (q, q, q, q, limit)).fetchall()
    return [dict(r) for r in rows]


def stock_value(con: sqlite3.Connection) -> list:
    """Valeur du stock (quantité × prix d'achat HT) par magasin — un résumé,
    pas un détail par produit (cf. stock_value_top pour ça)."""
    rows = con.execute(
        "SELECT s.store_id, "
        "       SUM(s.qte_stock * COALESCE(a.prixachatht, 0)) AS valeur, "
        "       SUM(s.qte_stock) AS qte_totale, "
        "       COUNT(DISTINCT s.ref_art) AS nb_refs "
        "FROM stock_snapshot s JOIN article a "
        "  ON s.store_id=a.store_id AND s.ref_art=a.ref_art "
        "GROUP BY s.store_id").fetchall()
    return [dict(r) for r in rows]


def stock_value_top(con: sqlite3.Connection, store_id: int | None = None,
                    limit: int = 30) -> list:
    """Produits qui pèsent le plus dans la valeur du stock (qté × prix d'achat),
    dépôts cumulés par magasin."""
    sql = ("SELECT a.store_id, a.ref_art, a.designation, "
           "       SUM(s.qte_stock) AS qte_stock, a.prixachatht, "
           "       (SUM(s.qte_stock) * COALESCE(a.prixachatht, 0)) AS valeur "
           "FROM stock_snapshot s JOIN article a "
           "  ON s.store_id=a.store_id AND s.ref_art=a.ref_art ")
    params: list = []
    if store_id:
        sql += "WHERE a.store_id=? "
        params.append(store_id)
    sql += ("GROUP BY a.store_id, a.ref_art HAVING SUM(s.qte_stock) > 0 "
           "ORDER BY valeur DESC LIMIT ?")
    params.append(limit)
    rows = con.execute(sql, params).fetchall()
    return [dict(r) for r in rows]


def low_stock(con: sqlite3.Connection, threshold: int = 3, limit: int = 300) -> list:
    """Produits réellement proches de la rupture : quantité cumulée du magasin
    entre 0 et threshold (bornes incluses), les plus bas d'abord. Exclut le
    stock NÉGATIF (toujours une anomalie de données, cf. negative_stock) : le
    mélanger ici noyait les produits à réapprovisionner sous des écarts pouvant
    atteindre des milliers d'unités."""
    rows = con.execute(
        "SELECT a.store_id, a.ref_art, a.designation, "
        "       COALESCE(SUM(s.qte_stock), 0) AS qte_stock "
        "FROM article a LEFT JOIN stock_snapshot s "
        "  ON s.store_id=a.store_id AND s.ref_art=a.ref_art "
        "GROUP BY a.store_id, a.ref_art "
        "HAVING qte_stock >= 0 AND qte_stock <= ? "
        "ORDER BY qte_stock ASC, a.designation LIMIT ?",
        (threshold, limit)).fetchall()
    return [dict(r) for r in rows]


def stock_by_ref(con: sqlite3.Connection, store_id: int, ref_art: str) -> float | None:
    """Stock ACTUEL (cumulé sur tous les dépôts, positif ou négatif) d'un
    article dans UN magasin — miroir (stock_snapshot), pas de connexion
    Firebird live nécessaire. None si cet article n'a aucune ligne de stock
    connue pour ce magasin (pas encore synchronisé / n'existe pas), à
    distinguer d'un stock de 0 (article connu, réellement vide)."""
    row = con.execute(
        "SELECT SUM(qte_stock) FROM stock_snapshot WHERE store_id=? AND ref_art=?",
        (store_id, ref_art)).fetchone()
    return float(row[0]) if row and row[0] is not None else None


def negative_stock(con: sqlite3.Connection, limit: int = 300) -> list:
    """Stock NÉGATIF cumulé par magasin — toujours une ANOMALIE de données
    (retour non rapproché, inventaire mal saisi, mouvement de stock au
    mauvais sens...), jamais un vrai niveau à réapprovisionner. Séparé de
    low_stock() pour ne pas noyer les produits réellement proches de zéro
    sous des écarts qui peuvent atteindre des milliers d'unités."""
    rows = con.execute(
        "SELECT a.store_id, a.ref_art, a.designation, "
        "       COALESCE(SUM(s.qte_stock), 0) AS qte_stock "
        "FROM article a LEFT JOIN stock_snapshot s "
        "  ON s.store_id=a.store_id AND s.ref_art=a.ref_art "
        "GROUP BY a.store_id, a.ref_art "
        "HAVING qte_stock < 0 "
        "ORDER BY qte_stock ASC, a.designation LIMIT ?",
        (limit,)).fetchall()
    return [dict(r) for r in rows]


def transfer_suggestions(con: sqlite3.Connection, min_surplus: int = 4,
                         limit: int = 200) -> list:
    """Suggestions de transfert : un magasin en rupture d'un produit alors qu'un
    AUTRE magasin a un surplus du MÊME produit (regroupé via match_key — voir
    stock_search). Purement basé sur le déséquilibre de stock actuel (pas sur
    l'historique de ventes) : une piste à vérifier, pas une décision automatique."""
    rows = con.execute(
        "SELECT a.ref_art, a.designation, a.store_id, "
        "       " + _MATCH_KEY_SQL + ", "
        "       COALESCE(SUM(s.qte_stock), 0) AS qte_stock "
        "FROM article a LEFT JOIN stock_snapshot s "
        "  ON s.store_id=a.store_id AND s.ref_art=a.ref_art "
        "GROUP BY a.store_id, a.ref_art").fetchall()

    by_key: dict = {}
    for r in rows:
        by_key.setdefault(r["match_key"], []).append(dict(r))

    out = []
    for items in by_key.values():
        if len(items) < 2:
            continue
        low = [it for it in items if (it["qte_stock"] or 0) <= 0]
        high = [it for it in items if (it["qte_stock"] or 0) >= min_surplus]
        if not low or not high:
            continue
        high.sort(key=lambda it: it["qte_stock"], reverse=True)
        source = high[0]
        for target in low:
            suggested = max(1, (source["qte_stock"] - 2) // 2)
            out.append({
                "designation": items[0]["designation"],
                "from_store_id": source["store_id"], "from_ref": source["ref_art"],
                "from_qty": source["qte_stock"],
                "to_store_id": target["store_id"], "to_ref": target["ref_art"],
                "suggested_qty": suggested,
            })
    out.sort(key=lambda r: -r["from_qty"])
    return out[:limit]


def ventes_rows(con: sqlite3.Connection, days: int = 7, limit: int = 300) -> list:
    rows = con.execute(
        "SELECT p.store_id, p.datepiece, p.nopiece, "
        "  COALESCE(t.raison_sociale, p.code_tiers) AS client, "
        "  p.montantttc, p.code_mode_regl "
        "FROM piece p LEFT JOIN tiers t "
        "  ON p.store_id=t.store_id AND p.code_tiers=t.code_tiers "
        "WHERE p.datepiece >= date('now', ?) "
        "  AND p.code_type_piece IN " + _VENTE_SQL + " "
        "ORDER BY p.datepiece DESC LIMIT ?", ("-%d days" % days, limit)).fetchall()
    return [dict(r) for r in rows]


def sales_by_product(con: sqlite3.Connection, days: int = 30, store_id: int | None = None,
                     order: str = "desc", limit: int = 50) -> list:
    """Quantité (et CA brut avant remise) vendue par produit sur les N derniers
    jours — ventes NETTES (une ligne de retour, qte négative, réduit le total,
    cf. le -1200 DA d'entrée constaté sur les retours). Pièces/lignes annulées
    et pièces hors allowlist (cf. VENTE_TYPES)
    exclues. order='desc' = meilleures ventes, 'asc' = ventes les plus faibles
    (produits toujours vendus, mais peu)."""
    sql = ("SELECT i.store_id, i.ref_art, "
           "       COALESCE(a.designation, i.ref_art) AS designation, "
           "       SUM(i.qte) AS qte_total, SUM(i.qte * i.prixht) AS ca_brut "
           "FROM item i "
           "JOIN piece p ON i.store_id=p.store_id AND i.nopiece=p.nopiece "
           "LEFT JOIN article a ON a.store_id=i.store_id AND a.ref_art=i.ref_art "
           "WHERE p.datepiece >= date('now', ?) "
           "  AND (i.annulee IS NULL OR i.annulee=0) "
           "  AND (p.annulee IS NULL OR p.annulee=0) "
           "  AND p.code_type_piece IN " + _VENTE_SQL + " ")
    params: list = ["-%d days" % days]
    if store_id:
        sql += "AND i.store_id=? "
        params.append(store_id)
    sql += ("GROUP BY i.store_id, i.ref_art "
           "ORDER BY qte_total %s LIMIT ?" % ("DESC" if order != "asc" else "ASC"))
    params.append(limit)
    rows = con.execute(sql, params).fetchall()
    return [dict(r) for r in rows]


def dead_stock(con: sqlite3.Connection, days: int = 30, store_id: int | None = None,
              limit: int = 200) -> list:
    """Produits en stock mais SANS aucune vente (qte > 0 sur une ligne non
    annulée, cf. VENTE_TYPES) depuis N
    jours, dans ce magasin — pièces/lignes annulées exclues.

    Calcule l'ensemble « vendu récemment » en UNE passe (CTE), puis fait un
    anti-join dessus — plutôt qu'une sous-requête NOT EXISTS corrélée exécutée
    pour chaque article un par un, bien plus lente sur un catalogue réel de
    plusieurs milliers de références."""
    sql = ("WITH sold AS ("
           "  SELECT DISTINCT i.store_id, i.ref_art "
           "  FROM item i JOIN piece p "
           "    ON i.store_id=p.store_id AND i.nopiece=p.nopiece "
           "  WHERE p.datepiece >= date('now', ?) "
           "    AND (i.annulee IS NULL OR i.annulee=0) "
           "    AND (p.annulee IS NULL OR p.annulee=0) "
           "    AND p.code_type_piece IN " + _VENTE_SQL + " "
           "    AND i.qte > 0"
           ") "
           "SELECT a.store_id, a.ref_art, a.designation, "
           "       COALESCE(SUM(s.qte_stock), 0) AS qte_stock "
           "FROM article a "
           "LEFT JOIN stock_snapshot s "
           "  ON s.store_id=a.store_id AND s.ref_art=a.ref_art "
           "LEFT JOIN sold "
           "  ON sold.store_id=a.store_id AND sold.ref_art=a.ref_art "
           "WHERE sold.store_id IS NULL ")
    params: list = ["-%d days" % days]
    if store_id:
        sql += "AND a.store_id=? "
        params.append(store_id)
    sql += ("GROUP BY a.store_id, a.ref_art HAVING qte_stock > 0 "
           "ORDER BY qte_stock DESC LIMIT ?")
    params.append(limit)
    rows = con.execute(sql, params).fetchall()
    return [dict(r) for r in rows]


def ventes_range(con: sqlite3.Connection, date_from: str, date_to: str,
                 store_id: int | None = None) -> list:
    """Chiffre d'affaires PAR JOUR ET PAR MAGASIN sur une période [date_from,
    date_to] (bornes incluses) — pièces annulées et pièces non-ventes (BDR,
    pièces hors allowlist (cf. VENTE_TYPES) exclues. Sert à comparer les
    magasins entre eux jour par jour."""
    sql = ("SELECT store_id, substr(datepiece, 1, 10) AS jour, "
           "       SUM(montantttc) AS ca, COUNT(*) AS nb_pieces "
           "FROM piece "
           "WHERE datepiece BETWEEN ? AND ? "
           "  AND (annulee IS NULL OR annulee=0) "
           "  AND code_type_piece IN " + _VENTE_SQL + " ")
    params: list = [date_from, date_to + " 23:59:59"]
    if store_id:
        sql += "AND store_id=? "
        params.append(store_id)
    sql += "GROUP BY store_id, jour ORDER BY jour DESC, store_id"
    rows = con.execute(sql, params).fetchall()
    return [dict(r) for r in rows]


def piece_type_breakdown(con: sqlite3.Connection) -> list:
    """Chaque (magasin, code_type_piece) réellement présent dans les données
    synchronisées (tout historique), avec son nombre de pièces, combien ont un
    MONTANTTTC non-nul (nb_avec_montant) contre le total (nb_pieces), le
    montant TTC cumulé, sa désignation (mirror TYPE_PIECE si connue), et si ce
    code est actuellement traité comme une VENTE (VENTE_TYPES).

    Groupé PAR MAGASIN (pas seulement par type) : deux magasins peuvent avoir
    le même type de pièce mais un MONTANTTTC synchronisé pour l'un et pas
    l'autre (variante de schéma Firebird selon l'installation/version), ce qui
    produirait un magasin à 0,00 DA de CA malgré des pièces bien présentes —
    invisible si on agrège tous les magasins ensemble."""
    rows = con.execute(
        "SELECT p.store_id, p.code_type_piece, "
        "       MAX(tp.designation) AS designation, "
        "       COUNT(*) AS nb_pieces, "
        "       SUM(CASE WHEN p.montantttc IS NOT NULL AND p.montantttc <> 0 "
        "                THEN 1 ELSE 0 END) AS nb_avec_montant, "
        "       SUM(p.montantttc) AS total_montantttc, "
        "       MIN(p.datepiece) AS premiere_date, "
        "       MAX(p.datepiece) AS derniere_date "
        "FROM piece p LEFT JOIN type_piece tp "
        "  ON tp.store_id = p.store_id AND tp.code_type_piece = p.code_type_piece "
        "GROUP BY p.store_id, p.code_type_piece "
        "ORDER BY p.store_id, nb_pieces DESC").fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["is_vente"] = d["code_type_piece"] in VENTE_TYPES
        out.append(d)
    return out


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


# SQL du match_key réutilisé partout où l'on regroupe un article multi-magasin
# (article_search, price_sync_candidates) : priorité à un lien manuel confirmé
# (article_link — cf. confirm_article_link), puis au code-barres équivalent
# partagé (EQUIV_CBARRES), puis à la référence brute en dernier recours.
_MATCH_KEY_SQL = (
    "COALESCE("
    "  (SELECT link_key FROM article_link l WHERE l.store_id=a.store_id AND l.ref_art=a.ref_art), "
    "  (SELECT MIN(e.code_barres) FROM equiv_cbarres e WHERE e.store_id=a.store_id AND e.ref_art=a.ref_art), "
    "  a.ref_art"
    ") AS match_key"
)


def article_search(con: sqlite3.Connection, query: str = "", limit: int = 200) -> list:
    """Articles correspondant à la recherche, une ligne par (article, magasin).

    La recherche porte sur la référence, la désignation, le code-barres miroir
    de l'article ET les codes-barres équivalents (EQUIV_CBARRES — la table que
    le logiciel scanne réellement).

    Chaque ligne porte aussi `match_key` (cf. _MATCH_KEY_SQL) : un lien manuel
    confirmé (article_link) ou un code-barres équivalent partagé, ou à défaut
    la référence. Deux magasins qui vendent le MÊME produit sous des
    références différentes partagent ainsi la même clé et les clients peuvent
    regrouper leurs lignes en un seul article multi-magasins.
    """
    q = "%" + (query or "") + "%"
    rows = con.execute(
        "SELECT a.ref_art, a.designation, a.store_id, a.prixventeht, "
        "       a.prixventettc, a.prixttcpromo, a.activepromo, "
        "       a.datedebpromo, a.datefinpromo, "
        "       " + _MATCH_KEY_SQL + " "
        "FROM article a "
        "WHERE a.ref_art LIKE ? OR a.designation LIKE ? OR a.code_barres LIKE ? "
        "   OR EXISTS (SELECT 1 FROM equiv_cbarres e WHERE e.store_id=a.store_id "
        "              AND e.ref_art=a.ref_art AND e.code_barres LIKE ?) "
        "ORDER BY a.ref_art, a.store_id LIMIT ?", (q, q, q, q, limit)).fetchall()
    return [dict(r) for r in rows]


def refs_for_match_key(con: sqlite3.Connection, match_key: str) -> dict:
    """Référence de CHAQUE magasin pour une clé de regroupement d'article.

    `match_key` est un lien manuel confirmé, un code-barres équivalent
    partagé, ou directement une référence (cf. _MATCH_KEY_SQL). Le même
    produit pouvant porter une référence différente selon le magasin, une
    mise à jour multi-magasins doit cibler la référence propre à chacun."""
    out: dict = {}
    rows = con.execute(
        "SELECT store_id, ref_art FROM article_link WHERE link_key=?",
        (match_key,)).fetchall()
    for r in rows:
        out[r["store_id"]] = r["ref_art"]
    rows = con.execute(
        "SELECT store_id, MIN(ref_art) AS ref_art FROM equiv_cbarres "
        "WHERE code_barres=? GROUP BY store_id", (match_key,)).fetchall()
    for r in rows:
        out.setdefault(r["store_id"], r["ref_art"])
    rows = con.execute(
        "SELECT store_id, ref_art FROM article WHERE ref_art=?",
        (match_key,)).fetchall()
    for r in rows:
        out.setdefault(r["store_id"], r["ref_art"])
    return out


def name_match_suggestions(con: sqlite3.Connection, limit: int = 100) -> list:
    """Groupes d'articles qui portent la MÊME désignation dans au moins 2
    magasins mais ne partagent PAS encore de match_key (ni lien manuel, ni
    code-barres commun) — candidats à confirm_article_link().

    Ne mélange rien automatiquement : c'est une liste de suggestions à faire
    valider par l'utilisateur (deux magasins peuvent nommer deux produits
    différents de la même façon)."""
    rows = con.execute(
        "SELECT a.ref_art, a.designation, a.store_id, a.prixventeht, "
        "       " + _MATCH_KEY_SQL + ", "
        "       UPPER(TRIM(a.designation)) AS norm_name "
        "FROM article a "
        "WHERE a.designation IS NOT NULL AND TRIM(a.designation) <> ''"
    ).fetchall()
    by_name: dict = {}
    for r in rows:
        by_name.setdefault(r["norm_name"], []).append(dict(r))

    out = []
    for items in by_name.values():
        store_ids = [it["store_id"] for it in items]
        if len(set(store_ids)) < 2:
            continue
        if len(store_ids) != len(set(store_ids)):
            # Un même magasin a PLUSIEURS articles sous ce nom (doublon interne
            # à corriger côté Netfact2, pas une correspondance entre magasins) :
            # impossible à confirmer proprement (article_link n'accepte qu'UN
            # article par magasin et par lien) — on n'affiche pas ce groupe.
            continue
        if len({it["match_key"] for it in items}) < 2:
            continue   # déjà unifiés (lien manuel ou code-barres commun)
        for it in items:
            it.pop("norm_name", None)
            it.pop("match_key", None)
        out.append({"designation": items[0]["designation"], "items": items})
        if len(out) >= limit:
            break
    return out


class ArticleLinkConflict(Exception):
    """Le lien demandé collerait deux articles DIFFÉRENTS du même magasin sous
    une seule clé de regroupement — refusé (sinon la référence/le prix
    « source » de ce magasin deviendrait ambigu pour price_sync_candidates)."""


def confirm_article_link(con: sqlite3.Connection, members: list) -> str | None:
    """Déclare que plusieurs (store_id, ref_art) sont le MÊME produit.

    `members` : liste de {"store_id":, "ref_art":}. Si l'un des membres a déjà
    un link_key (ajout d'un 3e magasin à un lien existant), il est réutilisé
    pour tous ; sinon une nouvelle clé est générée. Renvoie le link_key.

    Lève ArticleLinkConflict si `members` contient deux références différentes
    du même magasin, ou si le groupe visé contient déjà un article différent
    pour un magasin de `members` — un même magasin ne peut avoir qu'UN article
    par groupe."""
    members = [m for m in (members or []) if m.get("store_id") and m.get("ref_art")]
    if not members:
        return None

    wanted: dict = {}
    for m in members:
        prev = wanted.setdefault(m["store_id"], m["ref_art"])
        if prev != m["ref_art"]:
            raise ArticleLinkConflict(
                "Deux références différentes du magasin %s (%s et %s) ne "
                "peuvent pas être liées comme un seul produit."
                % (m["store_id"], prev, m["ref_art"]))

    link_key = None
    for m in members:
        row = con.execute(
            "SELECT link_key FROM article_link WHERE store_id=? AND ref_art=?",
            (m["store_id"], m["ref_art"])).fetchone()
        if row:
            link_key = row["link_key"]
            break
    if not link_key:
        link_key = "lnk-" + uuid.uuid4().hex[:16]

    for row in con.execute(
            "SELECT store_id, ref_art FROM article_link WHERE link_key=?", (link_key,)):
        sid, ref = row["store_id"], row["ref_art"]
        if sid in wanted and wanted[sid] != ref:
            raise ArticleLinkConflict(
                "Le magasin %s a déjà l'article %s dans ce groupe : "
                "impossible d'y ajouter aussi %s." % (sid, ref, wanted[sid]))

    now = now_iso()
    for m in members:
        con.execute(
            "INSERT INTO article_link (store_id, ref_art, link_key, created_at) "
            "VALUES (?, ?, ?, ?) "
            "ON CONFLICT(store_id, ref_art) DO UPDATE SET link_key=excluded.link_key",
            (m["store_id"], m["ref_art"], link_key, now))
    con.commit()
    return link_key


def price_sync_candidates(con: sqlite3.Connection, source_store_id: int) -> list:
    """Écarts de prix de vente entre `source_store_id` et les autres magasins,
    pour chaque groupe d'articles unifié (match_key). Sert à « synchroniser les
    prix depuis un magasin » : chaque groupe où la source a un prix connu et un
    autre magasin a un prix DIFFÉRENT (ou absent) devient un candidat."""
    rows = con.execute(
        "SELECT a.ref_art, a.designation, a.store_id, a.prixventeht, "
        "       " + _MATCH_KEY_SQL +
        " FROM article a").fetchall()
    by_key: dict = {}
    for r in rows:
        by_key.setdefault(r["match_key"], []).append(dict(r))

    out = []
    for key, items in by_key.items():
        source = next((it for it in items if it["store_id"] == source_store_id), None)
        if not source or source["prixventeht"] is None:
            continue
        targets = [
            {"store_id": it["store_id"], "ref_art": it["ref_art"],
             "current_price": it["prixventeht"]}
            for it in items
            if it["store_id"] != source_store_id
            and it["prixventeht"] != source["prixventeht"]
        ]
        if targets:
            out.append({"match_key": key, "designation": source["designation"],
                        "source_ref": source["ref_art"],
                        "source_price": source["prixventeht"], "targets": targets})
    return out


def article_barcodes(con: sqlite3.Connection, ref_art: str) -> list:
    """Codes-barres équivalents connus d'un article, par magasin (dernier sync)."""
    rows = con.execute(
        "SELECT store_id, code_barres FROM equiv_cbarres WHERE ref_art=? "
        "ORDER BY store_id, code_barres", (ref_art,)).fetchall()
    return [dict(r) for r in rows]


def apply_barcode_ops_local(con: sqlite3.Connection, store_id: int, ops: list) -> None:
    """Répercute immédiatement des ajouts/suppressions de codes-barres sur le
    miroir central (pour que l'affichage reste cohérent après une écriture
    directe sur un magasin en ligne)."""
    for op in ops:
        ref = op.get("ref_art")
        bc = op.get("barcode")
        if not ref or not bc:
            continue
        if op.get("action") == "remove":
            con.execute("DELETE FROM equiv_cbarres WHERE store_id=? AND ref_art=? "
                        "AND code_barres=?", (store_id, ref, bc))
        else:
            con.execute("INSERT OR IGNORE INTO equiv_cbarres "
                        "(store_id, ref_art, code_barres, synced_at) VALUES (?,?,?,?)",
                        (store_id, ref, bc, now_iso()))
    con.commit()


# Champ Firebird (payload price_update) -> colonne du miroir article. Les
# écritures directes sur un magasin ne bumpent pas forcément ARTICLE.DATEMODIF,
# donc la synchro incrémentale ne rafraîchirait jamais ces valeurs : on met le
# miroir à jour ici, immédiatement après l'application de l'op.
_PRICE_MIRROR_COLS = {
    "PRIXVENTEHT": "prixventeht",
    "PRIXVENTETTC": "prixventettc",
    "PRIXTTCPROMO": "prixttcpromo",
    "ACTIVEPROMO": "activepromo",
    "DATEDEBPROMO": "datedebpromo",
    "DATEFINPROMO": "datefinpromo",
}


def apply_item_edit_local(con: sqlite3.Connection, store_id: int, edits: list) -> None:
    """Répercute une op item_edit (qte/prix d'une ligne déjà créée modifiée —
    synchro fournisseur) sur le miroir central : la ligne item, le prix
    d'achat de l'article si demandé, et le total de la pièce (recalculé à
    partir des lignes item du miroir — TVA non mirée sur item, montantttc
    approximé = montantht, cohérent avec le "TTC = HT (pas de TVA)" déjà
    utilisé ailleurs pour l'éditeur de prix ; corrigé au prochain sync complet)."""
    touched_pieces = set()
    for e in edits or []:
        nopiece, noitem = e.get("nopiece"), e.get("noitem")
        if not nopiece or not noitem:
            continue
        con.execute(
            "UPDATE item SET qte=?, prixht=?, synced_at=? "
            "WHERE store_id=? AND nopiece=? AND noitem=?",
            (e.get("qte"), e.get("prix"), now_iso(), store_id, nopiece, noitem))
        touched_pieces.add(nopiece)
        if e.get("maj_prix_achat"):
            row = con.execute(
                "SELECT ref_art FROM item WHERE store_id=? AND nopiece=? AND noitem=?",
                (store_id, nopiece, noitem)).fetchone()
            if row and row["ref_art"]:
                con.execute(
                    "UPDATE article SET prixachatht=?, synced_at=? "
                    "WHERE store_id=? AND ref_art=?",
                    (e.get("prix"), now_iso(), store_id, row["ref_art"]))
    for nopiece in touched_pieces:
        row = con.execute(
            "SELECT COALESCE(SUM(qte * prixht), 0) FROM item "
            "WHERE store_id=? AND nopiece=?", (store_id, nopiece)).fetchone()
        ht = round(float(row[0] or 0), 4)
        con.execute(
            "UPDATE piece SET montantht=?, montantttc=?, synced_at=? "
            "WHERE store_id=? AND nopiece=?",
            (ht, ht, now_iso(), store_id, nopiece))
    con.commit()


def apply_item_add_local(con: sqlite3.Connection, store_id: int, imp_result: dict) -> None:
    """Répercute une op item_add (nouvelles lignes ajoutées à une pièce déjà
    existante — synchro fournisseur) sur le miroir central : insère les
    nouvelles lignes item et pose le total déjà recalculé par add_items
    (exact, pas besoin de resommer depuis le miroir comme
    apply_item_edit_local — on a directement le résultat de l'écriture)."""
    nopiece = imp_result.get("nopiece")
    if not nopiece:
        return
    now = now_iso()
    for it in imp_result.get("items") or []:
        con.execute(
            "INSERT OR REPLACE INTO item "
            "(store_id, nopiece, noitem, ref_art, qte, prixht, synced_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (store_id, nopiece, it["noitem"], it["ref_art"], it["qte"], it["prix"], now))
    ht = imp_result.get("montant_ht")
    if ht is not None:
        con.execute(
            "UPDATE piece SET montantht=?, montantttc=?, synced_at=? "
            "WHERE store_id=? AND nopiece=?",
            (ht, imp_result.get("montant_ttc", ht), now, store_id, nopiece))
    con.commit()


def apply_price_changes_local(con: sqlite3.Connection, store_id: int,
                              changes: list) -> None:
    """Répercute une op price_update appliquée sur le miroir central (même
    logique que apply_barcode_ops_local pour les codes-barres)."""
    for change in changes or []:
        ref = change.get("ref0")
        values = change.get("values") or {}
        sets, params = [], []
        for field, col in _PRICE_MIRROR_COLS.items():
            if field in values:
                sets.append("%s=?" % col)
                params.append(values[field])
        if not ref or not sets:
            continue
        sets.append("synced_at=?")
        params += [now_iso(), store_id, ref]
        con.execute("UPDATE article SET %s WHERE store_id=? AND ref_art=?"
                    % ", ".join(sets), params)
    con.commit()


# --------------------------------------------------------------------------- #
#  Synchro fournisseur (bon de livraison fournisseur -> bon de réception)
# --------------------------------------------------------------------------- #
def fournisseur_mapping(con: sqlite3.Connection) -> dict:
    """{code_tiers: {"store_id":, "raison_sociale":}} — la correspondance
    client-fournisseur -> magasin destinataire, éditable sur le tableau de
    bord web."""
    rows = con.execute(
        "SELECT code_tiers, store_id, raison_sociale FROM fournisseur_tiers_map"
    ).fetchall()
    return {r["code_tiers"]: {"store_id": r["store_id"],
                              "raison_sociale": r["raison_sociale"]} for r in rows}


def fournisseur_set_mapping(con: sqlite3.Connection, code_tiers: str, store_id: int,
                            raison_sociale: str | None = None) -> None:
    con.execute(
        "INSERT INTO fournisseur_tiers_map (code_tiers, store_id, raison_sociale, created_at) "
        "VALUES (?, ?, ?, ?) "
        "ON CONFLICT(code_tiers) DO UPDATE SET store_id=excluded.store_id, "
        "  raison_sociale=excluded.raison_sociale",
        (code_tiers, store_id, raison_sociale, now_iso()))
    con.commit()


def fournisseur_delete_mapping(con: sqlite3.Connection, code_tiers: str) -> None:
    con.execute("DELETE FROM fournisseur_tiers_map WHERE code_tiers=?", (code_tiers,))
    con.commit()


def fournisseur_sync_state_get(con: sqlite3.Connection, store_id: int,
                               src_nopiece: str, src_noitem: str) -> dict | None:
    row = con.execute(
        "SELECT * FROM fournisseur_sync_state "
        "WHERE store_id=? AND src_nopiece=? AND src_noitem=?",
        (store_id, src_nopiece, src_noitem)).fetchone()
    return dict(row) if row else None


def fournisseur_sync_state_set(con: sqlite3.Connection, store_id: int, src_nopiece: str,
                               src_noitem: str, dest_ref_art: str, dest_nopiece: str,
                               dest_noitem: str, qte: float, prix: float) -> None:
    con.execute(
        "INSERT INTO fournisseur_sync_state "
        "(store_id, src_nopiece, src_noitem, dest_ref_art, dest_nopiece, dest_noitem, "
        " last_qte, last_prix, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(store_id, src_nopiece, src_noitem) DO UPDATE SET "
        "  dest_ref_art=excluded.dest_ref_art, dest_nopiece=excluded.dest_nopiece, "
        "  dest_noitem=excluded.dest_noitem, last_qte=excluded.last_qte, "
        "  last_prix=excluded.last_prix, updated_at=excluded.updated_at",
        (store_id, src_nopiece, src_noitem, dest_ref_art, dest_nopiece, dest_noitem,
         qte, prix, now_iso()))
    con.commit()


def fournisseur_known_dest_nopiece(con: sqlite3.Connection, store_id: int,
                                   src_nopiece: str) -> str | None:
    """Le NOPIECE de réception déjà créé pour ce bon de livraison fournisseur
    (src_nopiece), s'il en existe déjà un — via n'importe laquelle de ses
    lignes soeurs déjà synchronisées. Sert à décider, quand le fournisseur
    ajoute des articles à un bon déjà connu, d'AJOUTER ces lignes à cette
    réception plutôt que d'en créer une seconde pour le même bon."""
    row = con.execute(
        "SELECT dest_nopiece FROM fournisseur_sync_state "
        "WHERE store_id=? AND src_nopiece=? AND dest_nopiece <> '' "
        "LIMIT 1", (store_id, src_nopiece)).fetchone()
    return row["dest_nopiece"] if row else None


def fournisseur_dest_nopiece_by_refdoc(con: sqlite3.Connection, store_id: int,
                                       src_nopiece: str) -> str | None:
    """Repli quand fournisseur_sync_state ne connaît PAS (encore) le NOPIECE
    créé pour ce BL : arrive quand la réception a été créée HORS LIGNE (mise
    en file) puis appliquée par l'agent — rien ne revient alors mettre à jour
    fournisseur_sync_state, ce champ reste vide pour toujours sans ce repli
    (édition ultérieure bloquée en 'pending_creation', et un article ajouté
    plus tard au même BL créerait une SECONDE réception au lieu de rejoindre
    celle-ci). apply_new_line stocke le NOPIECE du BL d'origine dans
    PIECE.REFDOC à la création (voir son docstring) ; ce champ arrive dans le
    miroir via la synchro régulière de l'agent (aucune action supplémentaire
    nécessaire — se corrige tout seul dès le prochain passage de l'agent)."""
    row = con.execute(
        "SELECT nopiece FROM piece WHERE store_id=? AND refdoc=? "
        "ORDER BY id DESC LIMIT 1", (store_id, src_nopiece)).fetchone()
    return row["nopiece"] if row else None


def fournisseur_dest_noitem_by_ref(con: sqlite3.Connection, store_id: int,
                                   nopiece: str, ref_art: str) -> str | None:
    """Le NOITEM d'une ligne déjà créée dans une réception connue via le
    repli REFDOC ci-dessus (nécessaire pour item_edit, qui a besoin du
    NOITEM précis à modifier, pas seulement du NOPIECE de la pièce)."""
    row = con.execute(
        "SELECT noitem FROM item WHERE store_id=? AND nopiece=? AND ref_art=? "
        "LIMIT 1", (store_id, nopiece, ref_art)).fetchone()
    # item.noitem est INTEGER dans le miroir (contrairement à
    # fournisseur_sync_state.dest_noitem et au reste du code, qui traitent
    # tous noitem comme une chaîne) — cast pour rester cohérent.
    return str(row["noitem"]) if row else None


def fournisseur_pending_add(con: sqlite3.Connection, store_id: int, src_nopiece: str,
                            src_noitem: str, ref_art: str | None, designation: str | None,
                            qte: float | None, prix: float | None,
                            code_barres: str | None, candidates: list,
                            tva: float | None = None) -> None:
    """Met en file une ligne à rapprocher manuellement (statut 'pending').

    L'exe fournisseur renvoie TOUTES les lignes à CHAQUE passage (voir sa
    docstring) : tant qu'une ligne reste non résolue par un humain, elle
    repasse ici à chaque synchro — on en profite pour RAFRAÎCHIR qte/prix/tva
    /désignation/candidats (le fournisseur a pu corriger le BL entre-temps),
    au lieu de figer les valeurs du tout premier passage. Ne touche jamais une
    ligne déjà 'resolved'/'ignored' (WHERE status='pending' sur l'UPDATE) —
    dans ce cas l'INSERT OR IGNORE qui suit est un no-op, la clé existe déjà."""
    cur = con.execute(
        "UPDATE fournisseur_pending SET ref_art=?, designation=?, qte=?, prix=?, tva=?, "
        " code_barres=?, candidates=? "
        "WHERE store_id=? AND src_nopiece=? AND src_noitem=? AND status='pending'",
        (ref_art, designation, qte, prix, tva, code_barres, json.dumps(candidates or []),
         store_id, src_nopiece, src_noitem))
    if cur.rowcount == 0:
        con.execute(
            "INSERT OR IGNORE INTO fournisseur_pending "
            "(store_id, src_nopiece, src_noitem, ref_art, designation, qte, prix, tva, "
            " code_barres, candidates, status, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?)",
            (store_id, src_nopiece, src_noitem, ref_art, designation, qte, prix, tva,
             code_barres, json.dumps(candidates or []), now_iso()))
    con.commit()


def _fournisseur_pending_row(row: sqlite3.Row) -> dict:
    d = dict(row)
    try:
        d["candidates"] = json.loads(d.get("candidates") or "[]")
    except (TypeError, ValueError):
        d["candidates"] = []
    resolution = d.get("resolution")
    if resolution:
        try:
            d["resolution"] = json.loads(resolution)
        except (TypeError, ValueError):
            pass
    return d


def fournisseur_pending_list(con: sqlite3.Connection, status: str = "pending") -> list:
    rows = con.execute(
        "SELECT * FROM fournisseur_pending WHERE status=? ORDER BY created_at",
        (status,)).fetchall()
    return [_fournisseur_pending_row(r) for r in rows]


def fournisseur_pending_get(con: sqlite3.Connection, pending_id: int) -> dict | None:
    row = con.execute("SELECT * FROM fournisseur_pending WHERE id=?", (pending_id,)).fetchone()
    return _fournisseur_pending_row(row) if row else None


def fournisseur_pending_resolve(con: sqlite3.Connection, pending_id: int, resolution: dict) -> None:
    con.execute(
        "UPDATE fournisseur_pending SET status='resolved', resolution=?, resolved_at=? WHERE id=?",
        (json.dumps(resolution), now_iso(), pending_id))
    con.commit()


def fournisseur_pending_ignore(con: sqlite3.Connection, pending_id: int) -> None:
    con.execute("UPDATE fournisseur_pending SET status='ignored', resolved_at=? WHERE id=?",
               (now_iso(), pending_id))
    con.commit()
