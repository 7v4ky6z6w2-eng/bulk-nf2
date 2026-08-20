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

    def tresorerie_today(self, day: str | None = None,
                         caisse: str | None = None) -> list:
        params = {}
        if day:
            params["day"] = day
        if caisse:
            params["caisse"] = caisse
        return self._get("/api/data/tresorerie", params).get("rows", [])

    def caisses(self) -> list:
        return self._get("/api/data/caisses").get("rows", [])

    def tresorerie_days(self) -> list:
        return self._get("/api/data/tresorerie_days").get("days", [])

    def tresorerie_range(self, date_from: str, date_to: str,
                         caisse: str | None = None,
                         store_id: int | None = None) -> list:
        params = {"from": date_from, "to": date_to}
        if caisse:
            params["caisse"] = caisse
        if store_id:
            params["store_id"] = store_id
        return self._get("/api/data/tresorerie_range", params).get("rows", [])

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

    def article_barcodes(self, ref_art: str) -> list:
        return self._get("/api/data/article_barcodes", {"ref": ref_art}).get("rows", [])

    def name_match_suggestions(self) -> list:
        return self._get("/api/data/name_match_suggestions").get("groups", [])

    def confirm_link(self, members: list) -> str | None:
        try:
            r = self._sess.post(self.base + "/api/data/confirm_link",
                                json={"members": members}, headers=self._headers(),
                                timeout=self.timeout)
        except requests.RequestException as exc:
            raise HubUnavailable(str(exc)) from exc
        if r.status_code == 400:
            # Conflit métier (ArticleLinkConflict côté hub) : message clair
            # dans le corps JSON, pas une erreur HTTP générique.
            try:
                raise HubUnavailable(r.json().get("error") or r.text)
            except ValueError:
                raise HubUnavailable(r.text) from None
        r.raise_for_status()
        return r.json().get("link_key")

    def price_sync_candidates(self, source_store_id: int) -> list:
        return self._get("/api/data/price_sync_candidates",
                         {"source_store_id": source_store_id}).get("groups", [])

    # -- écriture (BDR / prix / codes-barres) -------------------------------
    def submit_op(self, store_id: int, op_type: str, payload: dict,
                  timeout: float | None = None, op_uid: str | None = None) -> dict:
        """Envoie une opération au hub.

        Renvoie un dict : {"status": "applied"|"queued"|"error", ...}.
          * applied : appliqué tout de suite (magasin en ligne).
          * queued  : magasin hors ligne, mis en file (op_id renvoyé).
          * error   : échec de l'application directe (message dans "error").

        `op_uid` : uid d'idempotence — un retry (timeout réseau) avec le même
        uid renvoie le résultat déjà enregistré côté hub au lieu de ré-exécuter
        (indispensable pour bdr_import : évite un double import de stock).
        Les imports BDR peuvent être longs (gros fichier + sérialisation côté
        hub) : timeout par défaut 600 s pour eux, 180 s sinon.
        """
        if timeout is None:
            timeout = 600.0 if op_type == "bdr_import" else 180.0
        body = {"store_id": store_id, "op_type": op_type, "payload": payload}
        if op_uid:
            body["op_uid"] = op_uid
        try:
            r = self._sess.post(self.base + "/api/op", json=body,
                                headers=self._headers(), timeout=timeout)
            r.raise_for_status()
            return r.json()
        except requests.RequestException as exc:
            raise HubUnavailable(str(exc)) from exc
