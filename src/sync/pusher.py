"""Client HTTP de l'agent vers le hub.

Pousse les lectures (lots par table) et récupère / acquitte les opérations en
attente (pending_ops). Réessais avec backoff exponentiel sur erreurs réseau ou
5xx ; une 4xx (erreur de configuration) n'est pas réessayée.
"""

from __future__ import annotations

import time

import requests


class HubError(Exception):
    pass


class HubClient:
    def __init__(self, base_url: str, api_key: str = "", store_id: int = 0,
                 timeout: float = 120.0, retries: int = 3):
        self.base = base_url.rstrip("/")
        self.api_key = api_key
        self.store_id = store_id
        self.timeout = timeout
        self.retries = retries
        self._sess = requests.Session()

    def _headers(self) -> dict:
        h = {"X-Store-Id": str(self.store_id)}
        if self.api_key:
            h["X-Api-Key"] = self.api_key
        return h

    def _request(self, method: str, path: str, **kw):
        url = self.base + path
        delay = 2.0
        last = None
        for attempt in range(self.retries):
            try:
                r = self._sess.request(method, url, headers=self._headers(),
                                       timeout=self.timeout, **kw)
            except requests.RequestException as exc:
                last = exc
                if attempt < self.retries - 1:
                    time.sleep(delay); delay *= 2
                    continue
                raise HubError("Hub injoignable : %s" % exc) from exc
            if r.status_code < 400:
                return r
            if 400 <= r.status_code < 500:
                raise HubError("Hub a refusé (%d) : %s" % (r.status_code, r.text[:200]))
            # 5xx -> réessayer
            last = HubError("Hub erreur %d" % r.status_code)
            if attempt < self.retries - 1:
                time.sleep(delay); delay *= 2
        raise last or HubError("Échec inconnu")

    # -- API ---------------------------------------------------------------
    def ping(self) -> bool:
        try:
            self._request("GET", "/api/ping")
            return True
        except HubError:
            return False

    def start_sync(self, store_name: str = "") -> int:
        r = self._request("POST", "/api/sync/start",
                          json={"store_id": self.store_id, "store_name": store_name})
        return r.json()["session_id"]

    # Taille de lot : reste très en dessous de la limite de corps HTTP du hub
    # (25 Mo) même pour des lignes larges, et borne la mémoire côté hub.
    CHUNK_ROWS = 5000

    def push(self, session_id: int, table: str, rows: list) -> int:
        """Pousse une table par lots de CHUNK_ROWS lignes.

        Le PREMIER lot porte replace=true : pour les tables « instantané »
        (stock, codes-barres, trésorerie du jour), le hub purge l'état
        précédent du magasin avant d'insérer — les lignes disparues côté
        Firebird disparaissent aussi du miroir. Les lots suivants complètent
        sans re-purger.
        """
        if not rows:
            return 0
        accepted = 0
        for start in range(0, len(rows), self.CHUNK_ROWS):
            chunk = rows[start:start + self.CHUNK_ROWS]
            r = self._request("POST", "/api/sync/push/%s" % table,
                              json={"session_id": session_id,
                                    "store_id": self.store_id,
                                    "rows": chunk,
                                    "replace": start == 0})
            accepted += r.json().get("accepted", 0)
        return accepted

    def finish_sync(self, session_id: int, rows: int, status: str = "ok",
                    error_msg: str | None = None) -> None:
        self._request("POST", "/api/sync/finish",
                      json={"session_id": session_id, "rows": rows,
                            "status": status, "error_msg": error_msg})

    def pending_ops(self) -> list:
        r = self._request("GET", "/api/pending_ops",
                          params={"store_id": self.store_id})
        return r.json().get("ops", [])

    def report_op(self, op_id: int, ok: bool, error_msg: str | None = None) -> None:
        self._request("POST", "/api/pending_ops/%d/%s" % (op_id, "done" if ok else "failed"),
                      json={"error_msg": error_msg})
