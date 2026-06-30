"""Client de données HTTP du bureau vers le hub.

L'application bureau n'ouvre AUCUNE base locale et n'accède JAMAIS directement à
Firebird. Toutes les lectures (tableau de bord, recherche d'articles) et toutes
les écritures (import BDR, mise à jour de prix) passent par le hub via HTTP.

Conséquence : l'app tourne sur n'importe quel PC (même un PC personnel sans base
ni client Firebird), il suffit de `stores.json` (pour connaître l'URL du hub) et
d'un accès réseau au magasin 1.
"""

from __future__ import annotations

import requests


class HubUnavailable(Exception):
    pass


class HubData:
    def __init__(self, base_url: str, api_key: str = "", timeout: float = 20.0):
        self.base = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout = timeout
        self._sess = requests.Session()

    def _headers(self) -> dict:
        return {"X-Api-Key": self.api_key} if self.api_key else {}

    def _get(self, path: str, params: dict | None = None) -> dict:
        try:
            r = self._sess.get(self.base + path, params=params or {},
                               headers=self._headers(), timeout=self.timeout)
            r.raise_for_status()
            return r.json()
        except requests.RequestException as exc:
            raise HubUnavailable(str(exc)) from exc

    # -- santé -------------------------------------------------------------
    def ping(self) -> bool:
        try:
            self._get("/api/ping")
            return True
        except HubUnavailable:
            return False

    # -- lectures ----------------------------------------------------------
    def store_status(self) -> list:
        return self._get("/api/data/stores").get("rows", [])

    def tresorerie_today(self, day: str | None = None) -> list:
        return self._get("/api/data/tresorerie",
                         {"day": day} if day else {}).get("rows", [])

    def stock_rows(self, search: str = "") -> list:
        return self._get("/api/data/stock", {"q": search}).get("rows", [])

    def ventes_rows(self) -> list:
        return self._get("/api/data/ventes").get("rows", [])

    def sync_logs(self) -> list:
        return self._get("/api/data/sync_logs").get("rows", [])

    def pending_ops_recent(self) -> list:
        return self._get("/api/data/pending_ops_recent").get("rows", [])

    def article_search(self, query: str) -> list:
        return self._get("/api/data/article_search", {"q": query}).get("rows", [])

    # -- écriture (BDR / prix) --------------------------------------------
    def submit_op(self, store_id: int, op_type: str, payload: dict,
                  timeout: float = 180.0) -> dict:
        """Envoie une opération au hub.

        Renvoie un dict : {"status": "applied"|"queued"|"error", ...}.
          * applied : appliqué tout de suite (magasin en ligne).
          * queued  : magasin hors ligne, mis en file (op_id renvoyé).
          * error   : échec de l'application directe (message dans "error").
        """
        try:
            r = self._sess.post(
                self.base + "/api/op",
                json={"store_id": store_id, "op_type": op_type, "payload": payload},
                headers=self._headers(), timeout=timeout)
            r.raise_for_status()
            return r.json()
        except requests.RequestException as exc:
            raise HubUnavailable(str(exc)) from exc
