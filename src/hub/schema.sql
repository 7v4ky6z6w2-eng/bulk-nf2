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
    op_type       TEXT    NOT NULL,            -- bdr_import | price_update | barcode_ops
    payload       TEXT    NOT NULL,            -- JSON
    created_at    TEXT    NOT NULL,
    applied_at    TEXT,
    status        TEXT    DEFAULT 'pending',   -- pending | applied | failed
    error_msg     TEXT,
    notified      INTEGER DEFAULT 0,
    op_uid        TEXT                          -- uid d'idempotence (client), nullable
);
CREATE INDEX IF NOT EXISTS idx_ops_store_status ON pending_ops (store_id, status);
CREATE UNIQUE INDEX IF NOT EXISTS idx_ops_uid ON pending_ops (op_uid)
    WHERE op_uid IS NOT NULL;

-- Résultats des opérations appliquées DIRECTEMENT (magasin en ligne), indexés
-- par l'uid d'idempotence : un client qui ré-envoie le même op_uid (retry après
-- timeout réseau) reçoit le résultat enregistré au lieu de ré-exécuter l'import.
CREATE TABLE IF NOT EXISTS completed_ops (
    op_uid      TEXT PRIMARY KEY,
    result      TEXT NOT NULL,                 -- JSON du résultat renvoyé
    created_at  TEXT NOT NULL
);

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
    prixttcpromo  REAL,
    activepromo   INTEGER,
    datedebpromo  TEXT,
    datefinpromo  TEXT,
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
    caisse           TEXT    DEFAULT '(globale)',  -- caisse1, caisse2… (varie par base)
    sens             TEXT    DEFAULT 'entree', -- 'entree' (recette) | 'sortie' (dépense)
    mode_paiement    TEXT    NOT NULL,
    total_encaisse   REAL    DEFAULT 0,
    nb_transactions  INTEGER DEFAULT 0,
    synced_at        TEXT    NOT NULL,
    UNIQUE (store_id, snap_date, caisse, sens, mode_paiement)
);
CREATE INDEX IF NOT EXISTS idx_treso_store_date ON tresorerie_snapshot (store_id, snap_date);

-- Codes-barres équivalents (miroir de EQUIV_CBARRES, table affichée par Netfact2)
CREATE TABLE IF NOT EXISTS equiv_cbarres (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    store_id     INTEGER NOT NULL,
    ref_art      TEXT    NOT NULL,
    code_barres  TEXT    NOT NULL,
    synced_at    TEXT    NOT NULL,
    UNIQUE (store_id, ref_art, code_barres)
);
CREATE INDEX IF NOT EXISTS idx_equiv_ref ON equiv_cbarres (store_id, ref_art);
CREATE INDEX IF NOT EXISTS idx_equiv_bc  ON equiv_cbarres (code_barres);

-- Correspondances manuelles entre articles de magasins différents qui sont le
-- MÊME produit mais n'ont ni référence ni code-barres commun (ex. matché par
-- désignation puis confirmé par l'utilisateur). link_key est une clé de
-- regroupement arbitraire, égale pour tous les (store_id, ref_art) liés — voir
-- COALESCE dans article_search / price_sync_candidates de central_db.py.
CREATE TABLE IF NOT EXISTS article_link (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    store_id     INTEGER NOT NULL,
    ref_art      TEXT    NOT NULL,
    link_key     TEXT    NOT NULL,
    created_at   TEXT    NOT NULL,
    UNIQUE (store_id, ref_art)
);
CREATE INDEX IF NOT EXISTS idx_article_link_key ON article_link (link_key);

-- Empêche de renvoyer deux fois le même digest (ex. rupture de stock) le même
-- jour si le hub redémarre entre-temps.
CREATE TABLE IF NOT EXISTS digest_log (
    digest_type  TEXT NOT NULL,
    digest_date  TEXT NOT NULL,
    sent_at      TEXT NOT NULL,
    PRIMARY KEY (digest_type, digest_date)
);

-- ──────────────────── Synchro fournisseur (BL -> BDR) ──────────────────────
-- Correspondance CODE_TIERS (client dans le Firebird du fournisseur) ->
-- magasin destinataire. Éditable depuis le tableau de bord web ; alimentée au
-- premier lancement de l'outil de synchro (qui liste les clients de son
-- propre Firebird pour que l'utilisateur choisisse).
CREATE TABLE IF NOT EXISTS fournisseur_tiers_map (
    code_tiers      TEXT PRIMARY KEY,
    store_id        INTEGER NOT NULL,
    raison_sociale  TEXT,
    created_at      TEXT NOT NULL
);

-- Une ligne par ligne de BON DE LIVRAISON fournisseur déjà traitée, indexée
-- par la pièce/ligne D'ORIGINE (chez le fournisseur) : empêche de recréer une
-- ligne déjà importée, et retrouve la ligne de réception déjà créée côté
-- magasin quand le fournisseur modifie prix/qté après coup (édition en place
-- via l'opération item_edit).
CREATE TABLE IF NOT EXISTS fournisseur_sync_state (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    store_id       INTEGER NOT NULL,
    src_nopiece    TEXT    NOT NULL,        -- NOPIECE chez le fournisseur
    src_noitem     TEXT    NOT NULL,        -- NOITEM chez le fournisseur
    dest_ref_art   TEXT    NOT NULL,        -- référence retenue côté magasin
    dest_nopiece   TEXT    NOT NULL,        -- NOPIECE créé côté magasin
    dest_noitem    TEXT    NOT NULL,        -- NOITEM créé côté magasin
    last_qte       REAL,
    last_prix      REAL,
    updated_at     TEXT    NOT NULL,
    UNIQUE (store_id, src_nopiece, src_noitem)
);

-- Lignes de BL dont l'article n'a pu être rapproché qu'approximativement (par
-- désignation, pas par référence/code-barres) : en attente de décision
-- humaine sur le tableau de bord web — jamais appliquées automatiquement.
CREATE TABLE IF NOT EXISTS fournisseur_pending (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    store_id       INTEGER NOT NULL,
    src_nopiece    TEXT    NOT NULL,
    src_noitem     TEXT    NOT NULL,
    ref_art        TEXT,                    -- référence côté fournisseur
    designation    TEXT,
    qte            REAL,
    prix           REAL,
    tva            REAL,
    code_barres    TEXT,
    candidates     TEXT,                    -- JSON : candidats de rapprochement
    status         TEXT DEFAULT 'pending',  -- pending | resolved | ignored
    resolution     TEXT,                    -- JSON du choix retenu
    created_at     TEXT NOT NULL,
    resolved_at    TEXT,
    UNIQUE (store_id, src_nopiece, src_noitem)
);
CREATE INDEX IF NOT EXISTS idx_fourn_pending_status ON fournisseur_pending (status);
