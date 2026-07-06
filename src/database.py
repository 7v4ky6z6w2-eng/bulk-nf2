"""Accès à la base Netfact2 (Firebird 2.5) via le pilote `fdb`.

La base est hébergée sur un serveur de l'entreprise. On s'y connecte par le
réseau (TCP/3050). Un client Firebird récent peut dialoguer avec un serveur 2.5.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional, Tuple

import fdb

from config import FirebirdConfig


# Un palier de tarif par quantité : (qté min, qté max, prix unitaire HT).
QtyTier = Tuple[float, Optional[float], float]


@dataclass
class Article:
    ref_art: str
    designation: str
    prix_vente_ht: float
    tiers: List[QtyTier] = field(default_factory=list)


# Recherche d'un article à partir du code scanné :
#   1) on cherche d'abord par référence article (REF_ART = SKU WooCommerce) ;
#   2) sinon, on cherche le code dans la table des codes-barres équivalents
#      EQUIV_CBARRES (là où le logiciel Netfact enregistre les codes scannés),
#      puis on remonte à l'article par REF_ART.
# On ignore les articles « en sommeil » (désactivés).
_LOOKUP_SQL = """
SELECT FIRST 1 A.DESIGNATION, A.PRIXVENTEHT, A.REF_ART
FROM ARTICLE A
WHERE (A.EN_SOMMEIL = 0 OR A.EN_SOMMEIL IS NULL)
  AND ( A.REF_ART = ?
     OR A.REF_ART IN (SELECT E.REF_ART FROM EQUIV_CBARRES E WHERE E.CODE_BARRES = ?) )
"""


# Paliers de tarif par quantité (table TARIF de Netfact) pour un article.
# On ne garde que les vrais paliers « quantité » (qté min > 1) avec un prix
# renseigné. Ordonnés par quantité croissante.
_TIERS_SQL = """
SELECT T.QTEMIN, T.QTEMAX, T.PRIXHT
FROM TARIF T
WHERE T.REF_ART = ?
  AND T.PRIXHT > 0
  AND T.QTEMIN > 1
ORDER BY T.QTEMIN
"""


class DatabaseError(Exception):
    """Erreur de connexion ou de requête présentée à l'utilisateur."""


class Database:
    """Connexion Firebird avec reconnexion automatique."""

    def __init__(self, cfg: FirebirdConfig):
        self._cfg = cfg
        self._con: Optional[fdb.Connection] = None

    # --- connexion ---------------------------------------------------------
    def _connect(self) -> fdb.Connection:
        cfg = self._cfg
        try:
            return fdb.connect(
                host=cfg.host,
                port=cfg.port,
                database=cfg.database,
                user=cfg.user,
                password=cfg.password,
                charset="WIN1252",  # jeu de caractères des données Netfact
            )
        except Exception as exc:  # fdb.DatabaseError et autres
            raise DatabaseError(str(exc)) from exc

    def _ensure(self) -> fdb.Connection:
        if self._con is None or self._con.closed:
            self._con = self._connect()
        return self._con

    def close(self) -> None:
        if self._con is not None and not self._con.closed:
            try:
                self._con.close()
            finally:
                self._con = None

    # --- requêtes ----------------------------------------------------------
    def lookup_article(self, code: str) -> Optional[Article]:
        """Retourne l'article correspondant au code scanné, ou None si introuvable."""
        code = (code or "").strip()
        if not code:
            return None

        def _run() -> Optional[Article]:
            con = self._ensure()
            cur = con.cursor()
            cur.execute(_LOOKUP_SQL, (code, code))
            row = cur.fetchone()
            cur.close()
            if row is None:
                return None
            designation = (row[0] or "").strip()
            prix = float(row[1] or 0.0)
            ref_art = (row[2] or "").strip()
            article = Article(ref_art=ref_art, designation=designation,
                              prix_vente_ht=prix)
            article.tiers = self._qty_tiers(con, ref_art)
            return article

        try:
            return _run()
        except Exception:
            # Connexion possiblement tombée : on réessaie une fois.
            self.close()
            try:
                return _run()
            except Exception as exc:
                raise DatabaseError(str(exc)) from exc

    def _qty_tiers(self, con, ref_art: str) -> List[QtyTier]:
        """Retourne les paliers de tarif par quantité de l'article.

        Ne lève jamais : si la table TARIF est absente ou en erreur, on
        renvoie une liste vide (l'affichage prix/nom continue normalement).
        """
        if not ref_art:
            return []
        try:
            cur = con.cursor()
            cur.execute(_TIERS_SQL, (ref_art,))
            tiers: List[QtyTier] = []
            for row in cur.fetchall():
                qmin = float(row[0] or 0)
                qmax = float(row[1]) if row[1] is not None else None
                prix = float(row[2] or 0)
                if qmin > 1 and prix > 0:
                    tiers.append((qmin, qmax, prix))
            cur.close()
            return tiers
        except Exception:  # noqa: BLE001 — table absente / autre : on ignore
            return []

    def test_connection(self) -> None:
        """Vérifie que la connexion fonctionne (lève DatabaseError sinon)."""
        con = self._ensure()
        cur = con.cursor()
        cur.execute("SELECT 1 FROM RDB$DATABASE")
        cur.fetchone()
        cur.close()

    # --- Introspection (diagnostic schéma) ---------------------------------
    _SCHEMA_KEYWORDS = ("TARIF", "QTE", "QUANT", "GROS", "PRIX",
                        "REMISE", "PALIER", "PACK")

    def dump_schema(self) -> str:
        """Retourne un rapport texte : liste des tables, colonnes d'ARTICLE et
        des tables liées aux tarifs/quantités, avec quelques lignes d'exemple.

        Sert à découvrir la structure « tarif par quantité » sans la deviner.
        """
        con = self._ensure()
        cur = con.cursor()
        out = []

        cur.execute(
            "SELECT TRIM(RDB$RELATION_NAME) FROM RDB$RELATIONS "
            "WHERE RDB$SYSTEM_FLAG = 0 AND RDB$VIEW_BLR IS NULL "
            "ORDER BY RDB$RELATION_NAME"
        )
        tables = [r[0].strip() for r in cur.fetchall()]
        out.append("=== TABLES (%d) ===" % len(tables))
        out.append(", ".join(tables))

        def _columns(table: str):
            cur.execute(
                "SELECT TRIM(RDB$FIELD_NAME) FROM RDB$RELATION_FIELDS "
                "WHERE RDB$RELATION_NAME = ? ORDER BY RDB$FIELD_POSITION",
                (table,),
            )
            return [r[0].strip() for r in cur.fetchall()]

        targets = [t for t in tables
                   if t == "ARTICLE" or any(k in t for k in self._SCHEMA_KEYWORDS)]
        for t in targets:
            out.append("\n=== %s ===" % t)
            cols = _columns(t)
            out.append("colonnes : " + ", ".join(cols))
            # Quelques lignes d'exemple (uniquement tables de tarifs, non ARTICLE).
            if t != "ARTICLE":
                try:
                    cur.execute("SELECT FIRST 3 * FROM %s" % t)
                    rows = cur.fetchall()
                    for row in rows:
                        vals = ", ".join(
                            f"{c}={v!r}" for c, v in zip(cols, row))
                        out.append("  ex: " + vals)
                except Exception as exc:  # noqa: BLE001
                    out.append("  (exemple indisponible : %s)" % exc)

        cur.close()
        return "\n".join(out)
