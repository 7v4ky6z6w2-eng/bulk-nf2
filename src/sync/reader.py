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

import logging
from datetime import datetime, timedelta

import fdb

log = logging.getLogger("reader")


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
                            ("CODEFAMILLE", "codefamille"), ("DATEMODIF", "datemodif"),
                            ("PRIXTTCPROMO", "prixttcpromo"), ("ACTIVEPROMO", "activepromo"),
                            ("DATEDEBPROMO", "datedebpromo"), ("DATEFINPROMO", "datefinpromo")]:
            add(real, alias)
        sql = "SELECT " + ", ".join(sel) + " FROM ARTICLE"
        params = ()
        if since and _is_iso(since) and _candidate(cols, "DATEMODIF"):
            sql += " WHERE DATEMODIF > ? OR DATEMODIF IS NULL"
            # Firebird n'accepte pas le séparateur 'T' ISO 8601 en conversion
            # implicite chaîne -> timestamp (SQLCODE -303) : il faut un vrai
            # datetime Python, pas la chaîne JSON brute du fichier d'état.
            params = (datetime.fromisoformat(since),)
        return self._normalize_dates(self._fetch(sql, params), "datemodif",
                                     "datedebpromo", "datefinpromo")

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
        if since and _is_iso(since) and date_col:
            sql += " WHERE %s > ? OR %s IS NULL" % (date_col, date_col)
            # Même correctif que read_article() : Firebird refuse le 'T' ISO
            # 8601 en conversion implicite chaîne -> timestamp (SQLCODE -303).
            params = (datetime.fromisoformat(since),)
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

    # -- codes-barres équivalents (table affichée par Netfact2) -----------
    def read_equiv_cbarres(self) -> list:
        """Tous les codes-barres de EQUIV_CBARRES (REF_ART + CODE_BARRES).

        C'est la table que Netfact2 affiche dans la fiche article et utilise
        pour le scan (contrairement à ARTICLE.CODE_BARRE(35)/CODE_BARRES(60)).
        """
        if not self.has_table("EQUIV_CBARRES"):
            return []
        cols = self.columns("EQUIV_CBARRES")
        ref = _candidate(cols, "REF_ART")
        bc = _candidate(cols, "CODE_BARRES", "CODE_BARRE", "CBARRE")
        if not ref or not bc:
            return []
        return self._fetch(
            "SELECT %s AS ref_art, %s AS code_barres FROM EQUIV_CBARRES "
            "WHERE %s IS NOT NULL AND CHAR_LENGTH(TRIM(%s)) > 0" % (ref, bc, bc, bc))

    # -- stock (instantané) ------------------------------------------------
    def read_stock_snapshot(self) -> list:
        """Instantané du stock par article (et dépôt si disponible).

        Privilégie une vue/table dénormalisée si présente ; sinon appelle la
        procédure de stock du logiciel (SPSTOCKDEP / SPSTOCK) en UNE requête.
        Chaque branche vide est journalisée en WARNING : un stock absent du
        tableau de bord ne doit plus jamais être silencieux.
        """
        # 1) table/vue dénormalisée éventuelle
        for tbl in ("V_STOCK_DEPOT", "STOCK_DEPOT", "FICHE_STOCK"):
            if self.has_table(tbl):
                cols = self.columns(tbl)
                ref = _candidate(cols, "REF_ART")
                dep = _candidate(cols, "CODE_DEPOT")
                qte = _candidate(cols, "QTE_STOCK", "QTESTOCK", "QTE", "STOCK",
                                 "QUANTITE")
                pump = _candidate(cols, "PUMP", "PRIXSTOCK", "PRIX_REVIENT")
                if ref and dep and qte:
                    sel = ["%s AS ref_art" % ref, "%s AS code_depot" % dep,
                           "%s AS qte_stock" % qte]
                    sel.append(("%s AS pump" % pump) if pump else "NULL AS pump")
                    return self._fetch("SELECT %s FROM %s" % (", ".join(sel), tbl))
                log.warning("Stock : table %s présente mais colonnes attendues "
                            "introuvables (ref=%s, depot=%s, qte=%s) — ignorée.",
                            tbl, ref, dep, qte)
        # 2) procédure de stock du logiciel
        return self._stock_via_proc()

    # Valeur d'entrée par nom de paramètre pour l'appel « tout le stock » :
    #   * PREM_REF_ART vide = tous les articles (le source gère '' ET NULL via
    #     coalesce) ;
    #   * CODE_FAM doit être NULL, PAS '' : la procédure AllF (jointure des
    #     familles) ne renvoie TOUTES les familles que si son entrée est NULL —
    #     avec '' elle cherche la famille de code '' (inexistante) et ne
    #     renvoie rien, ce qui vide tout le stock (vérifié dans le source
    #     PSQL réel de la base de l'utilisateur) ;
    #   * GET_ITEM='0' = une ligne RÉSUMÉ par article (''/NULL ferait sortir la
    #     procédure sans AUCUNE ligne — même source) ;
    #   * DATEMIN/DATEMAX NULL = « aujourd'hui » côté procédure.
    # CODE_DEPOT : convention inconnue (''/NULL) — les deux sont tentées, voir
    # _stock_via_proc.
    _PROC_INPUT_VALUES = {
        "PREM_REF_ART": "", "REF_ART": "",
        "CODE_FAM": None, "CODEFAMILLE": None,
        "GET_ITEM": "0",
        "DATEMIN": None, "DATEMAX": None,
    }
    _DEPOT_PARAMS = ("CODE_DEPOT", "PARAM_CODE_DEPOT")

    def _proc_inputs(self, proc: str) -> list | None:
        """Noms des paramètres d'ENTRÉE d'une procédure, dans l'ordre d'appel.

        RDB$PARAMETER_TYPE : 0 = entrée, 1 = sortie. Renvoie None si la
        procédure n'existe pas."""
        if not _proc_exists(self.con, proc):
            return None
        cur = self.con.cursor()
        cur.execute(
            "SELECT TRIM(RDB$PARAMETER_NAME) FROM RDB$PROCEDURE_PARAMETERS "
            "WHERE TRIM(RDB$PROCEDURE_NAME) = ? AND RDB$PARAMETER_TYPE = 0 "
            "ORDER BY RDB$PARAMETER_NUMBER", (proc.upper(),))
        return [r[0] for r in cur.fetchall()]

    def _stock_via_proc(self) -> list:
        """Stock complet en UN appel à la procédure du logiciel.

        La signature varie selon la version Netfact2/PrimeOffice : sur
        PrimeOffice2026, SPSTOCKDEP prend 6 entrées (PREM_REF_ART, CODE_FAM,
        CODE_DEPOT, GET_ITEM, DATEMIN, DATEMAX) et renvoie une ligne par
        article (REF_ART, QTE_TOT, PUMP…). On introspecte donc les paramètres
        réels au lieu de supposer une arité fixe — l'ancien appel à 2
        arguments échouait sur CHAQUE article, silencieusement, d'où un stock
        éternellement vide au tableau de bord.

        La convention ''/NULL du paramètre dépôt n'étant pas connue pour
        toutes les versions, chaque procédure est tentée avec dépôt='' PUIS
        dépôt=NULL ; SPSTOCK (sans dépôt) sert de dernier recours. Le premier
        essai qui produit des lignes exploitables gagne.
        """
        tried = []
        for cand in ("SPSTOCKDEP", "SPSTOCK"):
            inputs = self._proc_inputs(cand)
            if inputs is None:
                continue
            has_depot = any(p.upper() in self._DEPOT_PARAMS for p in inputs)
            for depot_val in ("", None) if has_depot else (None,):
                out = self._call_stock_proc(cand, inputs, depot_val)
                if out:
                    return out
                tried.append("%s (dépôt=%r)" % (cand, depot_val)
                             if has_depot else cand)
        if tried:
            log.warning("Stock : aucune ligne exploitable — essais : %s. "
                        "Le stock restera vide au tableau de bord.",
                        " ; ".join(tried))
        else:
            log.warning("Stock : aucune procédure SPSTOCKDEP/SPSTOCK et aucune "
                        "table de stock — le stock restera vide au tableau de bord.")
        return []

    def _call_stock_proc(self, proc: str, inputs: list, depot_val) -> list:
        args = []
        for p in inputs:
            name = p.upper()
            if name in self._DEPOT_PARAMS:
                args.append(depot_val)
            else:
                args.append(self._PROC_INPUT_VALUES.get(name))
        sql = "SELECT * FROM %s(%s)" % (proc, ", ".join("?" * len(args))) \
            if args else "SELECT * FROM %s" % proc
        try:
            rows = self._fetch(sql, tuple(args))
        except fdb.Error as exc:
            log.warning("Stock : l'appel %s(%s) a échoué : %s", proc,
                        ", ".join(inputs), exc)
            return []

        out = []
        for rec in rows:
            ref = rec.get("ref_art")
            if not ref or not str(ref).strip():
                continue
            qte = None
            for k in ("qte_tot", "qte_stock", "qtestock", "qte", "stock"):
                if rec.get(k) is not None:
                    qte = rec[k]
                    break
            # Les quantités à 0 sont CONSERVÉES : une rupture de stock doit
            # rester visible au tableau de bord (0 en rouge), pas disparaître
            # comme si l'article n'existait plus.
            if qte is None:
                continue
            depot = str(rec.get("code_depot") or "").strip() or "(global)"
            out.append({"ref_art": str(ref).strip(), "code_depot": depot,
                        "qte_stock": qte,
                        "pump": rec.get("pump") or rec.get("prixstock")})
        return out

    # Types de pièce représentant un MOUVEMENT d'argent réel (encaissement /
    # décaissement / dépense) — confirmé par l'utilisateur sur sa base réelle.
    # Les pièces de vente (PC_VE_TIK, PC_VE_B…) sont volontairement EXCLUES :
    # leur paiement est enregistré séparément via une pièce PC_DV_VRS_EN liée
    # (PIECE.NOPIECE_O), pas sur la pièce de vente elle-même — les inclure
    # compterait le même paiement deux fois.
    _TRESO_TYPES_ENTREE = ("PC_DV_VRS_EN",)   # encaissement
    _TRESO_TYPES_SORTIE = ("PC_DV_VRS_SO", "PC_DV_DEP")  # décaissement, dépense/charge

    # -- trésorerie (encaissements du jour) --------------------------------
    def read_tresorerie_today(self, day: str | None = None) -> list:
        """Total encaissé aujourd'hui par CAISSE et par mode de règlement.

        Netfact2/PRIME enregistre les MOUVEMENTS d'argent (versements,
        dépenses…) comme des pièces à part, PAS comme un champ sur la pièce de
        vente — confirmé sur une base réelle (diag_columns.py) : ce sont les
        pièces de type PC_DV_VRS_EN (encaissement), PC_DV_VRS_SO
        (décaissement) et PC_DV_DEP (dépense/charge). Le montant réel est dans
        PIECE.MONTANT (PAS MONTANTVERSE, toujours à 0 sur ces mouvements).

        ANNULEE : sur CE produit, ANNULEE=1 = pièce ACTIVE (même convention
        que les BDR PC_AC_B) — vérifié sur des mouvements réels du jour, tous
        à ANNULEE=1. On ne filtre donc PAS dessus.

        Colonne « caisse » : absente de PIECE sur cette base (confirmé) — pas
        de panne, ce produit ne suit simplement pas cette dimension. On
        regroupe quand même par une colonne caisse SI elle existe (autre
        variante Netfact2), sinon tout tombe sous "(globale)".
        """
        cols = self.columns("PIECE")
        has_montant = _candidate(cols, "MONTANT")
        has_montantverse = _candidate(cols, "MONTANTVERSE")
        has_type = _candidate(cols, "CODE_TYPE_PIECE")
        if not (has_montant or has_montantverse) or not has_type:
            return []
        montant_col = "MONTANT" if has_montant else "MONTANTVERSE"

        day = day or datetime.now().strftime("%Y-%m-%d")
        name = self._name_col("MODE_REGL") or "DESIGNATION"
        has_mr = self.has_table("MODE_REGL")
        mode_expr = "MR.%s" % name if has_mr else "P.CODE_MODE_REGL"
        join = "JOIN MODE_REGL MR ON P.CODE_MODE_REGL = MR.CODE_MODE_REGL" if has_mr else ""

        # Colonne caisse (détectée : absente sur ce produit, présente sur d'autres).
        caisse_col = _candidate(cols, "CAISSE", "CODE_CAISSE", "NUM_CAISSE", "NOCAISSE")
        caisse_sel = "P.%s AS caisse, " % caisse_col if caisse_col else "'(globale)' AS caisse, "
        caisse_grp = ", P.%s" % caisse_col if caisse_col else ""

        # Sens dérivé du type de pièce (fiable, confirmé par l'utilisateur) ET
        # du signe du montant : Netfact2 enregistre parfois un RETOUR/AVOIR
        # comme une pièce de type "entrée" (PC_DV_VRS_EN) mais avec un montant
        # NÉGATIF (ex. -1200.00 DA) — le signe du champ, PAS le type, indique
        # alors que l'argent est réellement ressorti. Sans ce test, SUM(ABS())
        # ci-dessous effaçait le signe et comptait le retour comme une
        # RECETTE supplémentaire au lieu de le déduire (confirmé par
        # l'utilisateur sur sa base réelle). Un type "sortie" reste "sortie"
        # quel que soit le signe (une dépense est déjà positive côté Netfact2).
        entree_list = ",".join("'%s'" % t for t in self._TRESO_TYPES_ENTREE)
        type_in = ",".join("'%s'" % t for t in self._TRESO_TYPES_ENTREE + self._TRESO_TYPES_SORTIE)
        sens_expr = (
            "CASE WHEN P.CODE_TYPE_PIECE IN (%s) AND P.%s >= 0 THEN 'entree' "
            "ELSE 'sortie' END" % (entree_list, montant_col))

        sql = (
            "SELECT %s%s AS mode_paiement, %s AS sens, "
            "       SUM(ABS(P.%s)) AS total_encaisse, "
            "       COUNT(*) AS nb_transactions "
            "FROM PIECE P %s "
            "WHERE CAST(P.DATEPIECE AS DATE) = ? "
            "  AND P.CODE_TYPE_PIECE IN (%s) "
            "  AND P.%s IS NOT NULL AND P.%s <> 0 "
            "GROUP BY %s%s, %s" % (caisse_sel, mode_expr, sens_expr, montant_col, join,
                                   type_in, montant_col, montant_col,
                                   mode_expr, caisse_grp, sens_expr))
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
        yield "equiv_cbarres", self.read_equiv_cbarres()
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
