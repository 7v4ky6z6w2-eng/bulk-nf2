"""Accès à la base Netfact2 (Firebird 2.5) via le pilote `fdb`.

La base est hébergée sur un serveur de l'entreprise. On s'y connecte par le
réseau (TCP/3050). Un client Firebird récent peut dialoguer avec un serveur 2.5.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import fdb

from config import FirebirdConfig


@dataclass
class Article:
    ref_art: str
    designation: str
    prix_vente_ht: float


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
            return Article(ref_art=ref_art, designation=designation, prix_vente_ht=prix)

        try:
            return _run()
        except Exception:
            # Connexion possiblement tombée : on réessaie une fois.
            self.close()
            try:
                return _run()
            except Exception as exc:
                raise DatabaseError(str(exc)) from exc

    def test_connection(self) -> None:
        """Vérifie que la connexion fonctionne (lève DatabaseError sinon)."""
        con = self._ensure()
        cur = con.cursor()
        cur.execute("SELECT 1 FROM RDB$DATABASE")
        cur.fetchone()
        cur.close()
