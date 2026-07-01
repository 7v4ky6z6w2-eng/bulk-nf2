"""Lecture multi-tables de la base Firebird Netfact2/PRIME pour la synchro.

Conçu pour être robuste face aux variantes de schéma : les noms de colonnes
« libellé » (INTITULE / DESIGNATION / LIBELLE …) sont détectés à l'exécution via
les tables système RDB$, comme le fait l'éditeur primenf. Les requêtes ne
ramènent que les colonnes réellement présentes.

Stratégie de synchro par table :
  * référentiels courts (depot, type_piece, mode_regl, famille) → complet ;
  * article / tiers → incrémental sur DATEMODIF si la colonne existe ;
  * piece → incrémental sur DATEPIECE (avec marge de 2 jours) ;
  * item → jointure sur les pièces récentes ;
  * stock_snapshot → instantané via la procédure de stock si disponible ;
  * tresorerie_snapshot → encaissements du jour par mode de règlement.

Aucune écriture : connexion en lecture seule.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import fdb


# Types Firebird texte (pour repérer les colonnes libellé) : 14=CHAR, 37=VARCHAR.
def _candidate(cols: list, *names) -> str | None:
    up = {c.upper(): c for c in cols}
    for n in names:
        if n.upper() in up:
            return up[n.upper()]
    return None


class FirebirdReader:
    """Encapsule une connexion Firebird en lecture et les requêtes de synchro."""

    def __init__(self, connect_kwargs: dict):
        self._kwargs = dict(connect_kwargs)
        self.con = fdb.connect(**self._kwargs)
        self._cols_cache: dict[str, list] = {}

    def close(self) -> None:
        try:
            if self.con:
                self.con.close()
        finally:
            self.con = None

    # -- introspection -----------------------------------------------------
    def columns(self, table: str) -> list:
        """Noms réels des colonnes d'une table (ou [] si la table n'existe pas)."""
        key = table.upper()
        if key in self._cols_cache:
            return self._cols_cache[key]
        cur = self.con.cursor()
        cur.execute(
            "SELECT TRIM(rf.RDB$FIELD_NAME) "
            "FROM RDB$RELATION_FIELDS rf "
            "WHERE rf.RDB$RELATION_NAME = ? "
            "ORDER BY rf.RDB$FIELD_POSITION", (key,))
        cols = [r[0] for r in cur.fetchall()]
        self._cols_cache[key] = cols
        return cols

    def has_table(self, table: str) -> bool:
        return bool(self.columns(table))

    def _name_col(self, table: str) -> str | None:
        return _candidate(self.columns(table), "INTITULE", "DESIGNATION",
                          "LIBELLE", "NOM")

    # -- helpers -----------------------------------------------------------
    def _fetch(self, sql: str, params=()) -> list:
        cur = self.con.cursor()
        cur.execute(sql, params)
        cols = [d[0].lower() for d in cur.description]
        out = []
        for row in cur.fetchall():
            out.append({c: v for c, v in zip(cols, row)})
        return out

    @staticmethod
    def _iso(v):
        """Convertit date/datetime Firebird en chaîne ISO (ou laisse tel quel)."""
        if isinstance(v, (datetime,)):
            return v.isoformat(timespec="seconds")
        return v

    def _normalize_dates(self, rows: list, *fields) -> list:
        for r in rows:
            for f in fields:
                if f in r:
                    r[f] = self._iso(r[f])
        return rows

    # -- référentiels (synchro complète) -----------------------------------
    def read_famille(self) -> list:
        name = self._name_col("FAMILLE") or "INTITULE"
        code = _candidate(self.columns("FAMILLE"), "CODEFAMILLE", "CODE_FAM", "CODE")
        if not code:
            return []
        return self._fetch(
            "SELECT %s AS code_fam, %s AS designation FROM FAMILLE" % (code, name))

    def read_depot(self) -> list:
        if not self.has_table("DEPOT"):
            return []
        name = self._name_col("DEPOT") or "DESIGNATION"
        return self._fetch(
            "SELECT CODE_DEPOT AS code_depot, %s AS designation FROM DEPOT" % name)

    def read_type_piece(self) -> list:
        table = "TYPE_PIECE" if self.has_table("TYPE_PIECE") else (
            "LOCAL_TYPE_PIECE" if self.has_table("LOCAL_TYPE_PIECE") else None)
        if not table:
            return []
        name = self._name_col(table) or "INTITULE"
        return self._fetch(
            "SELECT CODE_TYPE_PIECE AS code_type_piece, %s AS designation FROM %s"
            % (name, table))

    def read_mode_regl(self) -> list:
        if not self.has_table("MODE_REGL"):
            return []
        name = self._name_col("MODE_REGL") or "DESIGNATION"
        return self._fetch(
            "SELECT CODE_MODE_REGL AS code_mode_regl, %s AS designation FROM MODE_REGL"
            % name)

    # -- article / tiers (incrémental DATEMODIF) ---------------------------
    def read_article(self, since: str | None = None) -> list:
        cols = self.columns("ARTICLE")
        sel = ["REF_ART AS ref_art"]
        def add(real, alias):
            if _candidate(cols, real):
                sel.append("%s AS %s" % (real, alias))
        for real, alias in [("DESIGNATION", "designation"), ("CODE_BARRES", "code_barres"),
                            ("PRIXVENTEHT", "prixventeht"), ("PRIXVENTETTC", "prixventettc"),
                            ("PRIXACHATHT", "prixachatht"), ("CTRLSTOCK", "ctrlstock"),
                            ("QTEMIN", "qtemin"), ("QTEMAX", "qtemax"),
                            ("CODEFAMILLE", "codefamille"), ("DATEMODIF", "datemodif")]:
            add(real, alias)
        sql = "SELECT " + ", ".join(sel) + " FROM ARTICLE"
        params = ()
        if since and _candidate(cols, "DATEMODIF"):
            sql += " WHERE DATEMODIF > ? OR DATEMODIF IS NULL"
            params = (since,)
        return self._normalize_dates(self._fetch(sql, params), "datemodif")

    def read_tiers(self, since: str | None = None) -> list:
        cols = self.columns("TIERS")
        sel = ["CODE_TIERS AS code_tiers"]
        if _candidate(cols, "RAISON_SOCIALE"):
            sel.append("RAISON_SOCIALE AS raison_sociale")
        date_col = _candidate(cols, "DATEMODIF", "DATE_CREATION")
        if date_col:
            sel.append("%s AS datemodif" % date_col)
        sql = "SELECT " + ", ".join(sel) + " FROM TIERS"
        params = ()
        if since and date_col:
            sql += " WHERE %s > ? OR %s IS NULL" % (date_col, date_col)
            params = (since,)
        return self._normalize_dates(self._fetch(sql, params), "datemodif")

    # -- pièces / lignes (ventes) ------------------------------------------
    def read_piece(self, since: str | None = None, buffer_days: int = 2) -> list:
        cols = self.columns("PIECE")
        sel = ["NOPIECE AS nopiece", "DATEPIECE AS datepiece",
               "CODE_TYPE_PIECE AS code_type_piece"]
        for real, alias in [("CODE_TIERS", "code_tiers"), ("CODE_DEPOT", "code_depot"),
                            ("MONTANTHT", "montantht"), ("MONTANTTTC", "montantttc"),
                            ("MONTANTVERSE", "montantverse"),
                            ("CODE_MODE_REGL", "code_mode_regl"), ("ANNULEE", "annulee")]:
            if _candidate(cols, real):
                sel.append("%s AS %s" % (real, alias))
        sql = "SELECT " + ", ".join(sel) + " FROM PIECE"
        params = ()
        if since:
            cutoff = (datetime.fromisoformat(since) - timedelta(days=buffer_days)) \
                if _is_iso(since) else None
            if cutoff:
                sql += " WHERE DATEPIECE >= ?"
                params = (cutoff,)
        return self._normalize_dates(self._fetch(sql, params), "datepiece")

    def read_item_for_recent(self, since: str | None = None, buffer_days: int = 2) -> list:
        cols = self.columns("ITEM")
        qte = _candidate(cols, "QTE", "QTEUNIT") or "QTE"
        sel = ["NOPIECE AS nopiece", "NOITEM AS noitem", "REF_ART AS ref_art",
               "%s AS qte" % qte, "PRIXHT AS prixht"]
        for real, alias in [("REMISE", "remise"), ("MARGE", "marge"), ("ANNULEE", "annulee")]:
            if _candidate(cols, real):
                sel.append("%s AS %s" % (real, alias))
        sql = "SELECT " + ", ".join(sel) + " FROM ITEM"
        params = ()
        if since and _is_iso(since):
            cutoff = datetime.fromisoformat(since) - timedelta(days=buffer_days)
            sql += (" WHERE NOPIECE IN (SELECT NOPIECE FROM PIECE WHERE DATEPIECE >= ?)")
            params = (cutoff,)
        return self._fetch(sql, params)

    # -- stock (instantané) ------------------------------------------------
    def read_stock_snapshot(self) -> list:
        """Instantané du stock par article et dépôt.

        Privilégie une vue/table dénormalisée si présente ; sinon tente la
        procédure SPSTOCKDEP par article×dépôt. Si rien n'est disponible,
        renvoie une liste vide (le stock n'apparaît pas au tableau de bord mais
        la synchro continue).
        """
        # 1) table/vue dénormalisée éventuelle
        for tbl in ("V_STOCK_DEPOT", "STOCK_DEPOT", "FICHE_STOCK"):
            if self.has_table(tbl):
                cols = self.columns(tbl)
                ref = _candidate(cols, "REF_ART")
                dep = _candidate(cols, "CODE_DEPOT")
                qte = _candidate(cols, "QTE_STOCK", "QTE", "STOCK", "QUANTITE")
                pump = _candidate(cols, "PUMP", "PRIXSTOCK", "PRIX_REVIENT")
                if ref and dep and qte:
                    sel = ["%s AS ref_art" % ref, "%s AS code_depot" % dep,
                           "%s AS qte_stock" % qte]
                    sel.append(("%s AS pump" % pump) if pump else "NULL AS pump")
                    return self._fetch("SELECT %s FROM %s" % (", ".join(sel), tbl))
        # 2) procédure stockée SPSTOCKDEP(ref, depot) — appelée par lot
        return self._stock_via_proc()

    def _stock_via_proc(self) -> list:
        if not _proc_exists(self.con, "SPSTOCKDEP"):
            return []
        refs = [r["ref_art"] for r in self._fetch("SELECT REF_ART AS ref_art FROM ARTICLE")]
        depots = [r["code_depot"] for r in self._fetch("SELECT CODE_DEPOT AS code_depot FROM DEPOT")] \
            if self.has_table("DEPOT") else []
        if not depots:
            return []
        out = []
        cur = self.con.cursor()
        for ref in refs:
            for dep in depots:
                try:
                    cur.execute("SELECT * FROM SPSTOCKDEP(?, ?)", (ref, dep))
                    row = cur.fetchone()
                except fdb.Error:
                    continue
                if not row:
                    continue
                desc = [d[0].lower() for d in cur.description]
                rec = {c: v for c, v in zip(desc, row)}
                qte = rec.get("qte_stock") or rec.get("qte") or rec.get("stock")
                if qte in (None, 0):
                    continue
                out.append({"ref_art": ref, "code_depot": dep,
                            "qte_stock": qte,
                            "pump": rec.get("pump") or rec.get("prixstock")})
        return out

    # -- trésorerie (encaissements du jour) --------------------------------
    def read_tresorerie_today(self, day: str | None = None) -> list:
        """Total encaissé aujourd'hui par CAISSE et par mode de règlement.

        Netfact2 stocke la caisse (caisse1, caisse2…) dans la colonne PIECE.CAISSE
        et ces valeurs varient d'une base à l'autre. On regroupe donc par
        (caisse, mode) ; le tableau de bord permet ensuite de choisir une caisse
        ou « Globale » (toutes cumulées).

        Note : sur les pièces de VENTE, ANNULEE=0 = active (le BDR utilise =1).
        On retient les pièces avec MONTANTVERSE renseigné et non annulées.
        """
        cols = self.columns("PIECE")
        if not _candidate(cols, "MONTANTVERSE") or not _candidate(cols, "CODE_MODE_REGL"):
            return []
        day = day or datetime.now().strftime("%Y-%m-%d")
        name = self._name_col("MODE_REGL") or "DESIGNATION"
        has_mr = self.has_table("MODE_REGL")
        mode_expr = "MR.%s" % name if has_mr else "P.CODE_MODE_REGL"
        join = "JOIN MODE_REGL MR ON P.CODE_MODE_REGL = MR.CODE_MODE_REGL" if has_mr else ""
        annulee_clause = "AND (P.ANNULEE = 0 OR P.ANNULEE IS NULL)" \
            if _candidate(cols, "ANNULEE") else ""

        # Colonne caisse (détectée : varie selon les installations).
        caisse_col = _candidate(cols, "CAISSE", "CODE_CAISSE", "NUM_CAISSE", "NOCAISSE")
        caisse_sel = "P.%s AS caisse, " % caisse_col if caisse_col else "'(globale)' AS caisse, "
        caisse_grp = ", P.%s" % caisse_col if caisse_col else ""

        # Sens entrée / sortie : Netfact2 encode le sens financier de la pièce
        # dans COEFF_PIECE (+ COEFF_PIECE_TR). Signe >= 0 → recette (entrée) ;
        # < 0 → dépense (sortie). À défaut, on se rabat sur le signe du montant
        # versé (certaines installations stockent les sorties en négatif).
        cp = _candidate(cols, "COEFF_PIECE")
        cptr = _candidate(cols, "COEFF_PIECE_TR")
        if cp:
            coeff = "COALESCE(P.%s,0)" % cp
            if cptr:
                coeff += " + COALESCE(P.%s,0)" % cptr
            sens_expr = "CASE WHEN (%s) < 0 THEN 'sortie' ELSE 'entree' END" % coeff
        else:
            sens_expr = "CASE WHEN P.MONTANTVERSE < 0 THEN 'sortie' ELSE 'entree' END"

        sql = (
            "SELECT %s%s AS mode_paiement, %s AS sens, "
            "       SUM(ABS(P.MONTANTVERSE)) AS total_encaisse, "
            "       COUNT(*) AS nb_transactions "
            "FROM PIECE P %s "
            "WHERE CAST(P.DATEPIECE AS DATE) = ? %s "
            "  AND P.MONTANTVERSE IS NOT NULL AND P.MONTANTVERSE <> 0 "
            "GROUP BY %s%s, %s" % (caisse_sel, mode_expr, sens_expr, join,
                                   annulee_clause, mode_expr, caisse_grp, sens_expr))
        rows = self._fetch(sql, (day,))
        for r in rows:
            r["snap_date"] = day
            r["mode_paiement"] = r.get("mode_paiement") or "(inconnu)"
            r["sens"] = r.get("sens") or "entree"
            r["caisse"] = (str(r.get("caisse")).strip() if r.get("caisse") is not None
                           else "(globale)") or "(globale)"
        return rows

    # -- orchestration -----------------------------------------------------
    def read_all(self, since: dict | None = None, full: bool = False):
        """Génère des couples (table_centrale, lignes) pour la synchro.

        `since` : dict {table: watermark_iso}. Ignoré si full=True.
        """
        since = since or {}
        def w(t):
            return None if full else since.get(t)

        yield "depot", self.read_depot()
        yield "type_piece", self.read_type_piece()
        yield "mode_regl", self.read_mode_regl()
        yield "famille", self.read_famille()
        yield "article", self.read_article(w("article"))
        yield "tiers", self.read_tiers(w("tiers"))
        yield "piece", self.read_piece(w("piece"))
        yield "item", self.read_item_for_recent(w("piece"))
        yield "stock_snapshot", self.read_stock_snapshot()
        yield "tresorerie_snapshot", self.read_tresorerie_today()


def _is_iso(s) -> bool:
    try:
        datetime.fromisoformat(s)
        return True
    except (TypeError, ValueError):
        return False


def _proc_exists(con, name: str) -> bool:
    cur = con.cursor()
    cur.execute("SELECT 1 FROM RDB$PROCEDURES WHERE TRIM(RDB$PROCEDURE_NAME) = ?",
                (name.upper(),))
    return cur.fetchone() is not None
