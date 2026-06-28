-- Schéma de la base centrale (central.db) — SQLite sur le poste hub (magasin 1).
-- Chaque table miroir porte store_id + synced_at. Les clés Firebird d'origine
-- sont conservées et rendues uniques par (store_id, clé) pour éviter les
-- collisions entre magasins. Aucune FK (les magasins se synchronisent dans un
-- ordre quelconque ; l'intégrité référentielle est « best effort » pour un
-- tableau de bord en lecture seule).

PRAGMA journal_mode = WAL;       -- lecture concurrente (dashboard) pendant l'écriture (agent)
PRAGMA foreign_keys = OFF;

-- ───────────────────────── Métadonnées de synchro ─────────────────────────
CREATE TABLE IF NOT EXISTS sync_log (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    store_id      INTEGER NOT NULL,
    started       TEXT    NOT NULL,
    finished      TEXT,
    rows_pushed   INTEGER DEFAULT 0,
    status        TEXT    DEFAULT 'running',   -- running | ok | error
    error_msg     TEXT
);

CREATE TABLE IF NOT EXISTS store_meta (
    store_id      INTEGER PRIMARY KEY,
    store_name    TEXT,
    last_seen     TEXT
);

-- File d'attente des écritures pour les magasins hors ligne.
CREATE TABLE IF NOT EXISTS pending_ops (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    store_id      INTEGER NOT NULL,
    op_type       TEXT    NOT NULL,            -- bdr_import | price_update
    payload       TEXT    NOT NULL,            -- JSON
    created_at    TEXT    NOT NULL,
    applied_at    TEXT,
    status        TEXT    DEFAULT 'pending',   -- pending | applied | failed
    error_msg     TEXT,
    notified      INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_ops_store_status ON pending_ops (store_id, status);

-- ───────────────────────────── ARTICLE ────────────────────────────────────
CREATE TABLE IF NOT EXISTS article (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    store_id      INTEGER NOT NULL,
    ref_art       TEXT    NOT NULL,
    designation   TEXT,
    code_barres   TEXT,
    prixventeht   REAL,
    prixventettc  REAL,
    prixachatht   REAL,
    ctrlstock     INTEGER,
    qtemin        REAL,
    qtemax        REAL,
    codefamille   TEXT,
    datemodif     TEXT,
    synced_at     TEXT    NOT NULL,
    UNIQUE (store_id, ref_art)
);
CREATE INDEX IF NOT EXISTS idx_article_store ON article (store_id);
CREATE INDEX IF NOT EXISTS idx_article_ref   ON article (ref_art);
CREATE INDEX IF NOT EXISTS idx_article_fam   ON article (codefamille);

-- ───────────────────────────── FAMILLE ────────────────────────────────────
CREATE TABLE IF NOT EXISTS famille (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    store_id      INTEGER NOT NULL,
    code_fam      TEXT    NOT NULL,
    designation   TEXT,
    synced_at     TEXT    NOT NULL,
    UNIQUE (store_id, code_fam)
);

-- ──────────────────────── TIERS (clients/fournisseurs) ─────────────────────
CREATE TABLE IF NOT EXISTS tiers (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    store_id       INTEGER NOT NULL,
    code_tiers     TEXT    NOT NULL,
    raison_sociale TEXT,
    datemodif      TEXT,
    synced_at      TEXT    NOT NULL,
    UNIQUE (store_id, code_tiers)
);
CREATE INDEX IF NOT EXISTS idx_tiers_store ON tiers (store_id);

-- ───────────────────────────── DEPOT ──────────────────────────────────────
CREATE TABLE IF NOT EXISTS depot (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    store_id      INTEGER NOT NULL,
    code_depot    TEXT    NOT NULL,
    designation   TEXT,
    synced_at     TEXT    NOT NULL,
    UNIQUE (store_id, code_depot)
);

-- ─────────────────────────── TYPE_PIECE ───────────────────────────────────
CREATE TABLE IF NOT EXISTS type_piece (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    store_id         INTEGER NOT NULL,
    code_type_piece  TEXT    NOT NULL,
    designation      TEXT,
    synced_at        TEXT    NOT NULL,
    UNIQUE (store_id, code_type_piece)
);

-- ─────────────────────────── MODE_REGL ────────────────────────────────────
CREATE TABLE IF NOT EXISTS mode_regl (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    store_id         INTEGER NOT NULL,
    code_mode_regl   TEXT    NOT NULL,
    designation      TEXT,
    synced_at        TEXT    NOT NULL,
    UNIQUE (store_id, code_mode_regl)
);

-- ──────────────────────── PIECE (entêtes ventes) ──────────────────────────
CREATE TABLE IF NOT EXISTS piece (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    store_id         INTEGER NOT NULL,
    nopiece          TEXT    NOT NULL,
    datepiece        TEXT,
    code_type_piece  TEXT,
    code_tiers       TEXT,
    code_depot       TEXT,
    montantht        REAL,
    montantttc       REAL,
    montantverse     REAL,
    code_mode_regl   TEXT,
    annulee          INTEGER,
    synced_at        TEXT    NOT NULL,
    UNIQUE (store_id, nopiece)
);
CREATE INDEX IF NOT EXISTS idx_piece_store ON piece (store_id);
CREATE INDEX IF NOT EXISTS idx_piece_date  ON piece (datepiece);
CREATE INDEX IF NOT EXISTS idx_piece_type  ON piece (code_type_piece);

-- ──────────────────────── ITEM (lignes ventes) ────────────────────────────
CREATE TABLE IF NOT EXISTS item (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    store_id      INTEGER NOT NULL,
    nopiece       TEXT    NOT NULL,
    noitem        INTEGER NOT NULL,
    ref_art       TEXT,
    qte           REAL,
    prixht        REAL,
    remise        REAL,
    marge         REAL,
    annulee       INTEGER,
    synced_at     TEXT    NOT NULL,
    UNIQUE (store_id, nopiece, noitem)
);
CREATE INDEX IF NOT EXISTS idx_item_store ON item (store_id);
CREATE INDEX IF NOT EXISTS idx_item_art   ON item (ref_art);
CREATE INDEX IF NOT EXISTS idx_item_piece ON item (nopiece);

-- ─────────────────────── STOCK (instantané par dépôt) ─────────────────────
CREATE TABLE IF NOT EXISTS stock_snapshot (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    store_id      INTEGER NOT NULL,
    ref_art       TEXT    NOT NULL,
    code_depot    TEXT    NOT NULL,
    qte_stock     REAL,
    pump          REAL,
    synced_at     TEXT    NOT NULL,
    UNIQUE (store_id, ref_art, code_depot)
);
CREATE INDEX IF NOT EXISTS idx_stock_store ON stock_snapshot (store_id);
CREATE INDEX IF NOT EXISTS idx_stock_art   ON stock_snapshot (ref_art);

-- ───────────────── TRÉSORERIE (encaissements du jour) ─────────────────────
CREATE TABLE IF NOT EXISTS tresorerie_snapshot (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    store_id         INTEGER NOT NULL,
    snap_date        TEXT    NOT NULL,         -- AAAA-MM-JJ
    mode_paiement    TEXT    NOT NULL,
    total_encaisse   REAL    DEFAULT 0,
    nb_transactions  INTEGER DEFAULT 0,
    synced_at        TEXT    NOT NULL,
    UNIQUE (store_id, snap_date, mode_paiement)
);
CREATE INDEX IF NOT EXISTS idx_treso_store_date ON tresorerie_snapshot (store_id, snap_date);
