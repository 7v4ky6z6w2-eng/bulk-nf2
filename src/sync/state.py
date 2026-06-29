"""Persistance des « watermarks » (repères incrémentaux) de l'agent.

Écrit dans un fichier JSON pour pouvoir reprendre après un arrêt/redémarrage du
poste. Écriture atomique (fichier .tmp puis os.replace) pour éviter la corruption
si le poste s'éteint en pleine écriture.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone

DEFAULT_WATERMARK = "1900-01-01T00:00:00"


class StateManager:
    def __init__(self, path: str):
        self.path = path
        self.data = {"store_id": None, "watermarks": {}, "last_sync_ok": None}
        self.load()

    def load(self) -> None:
        if os.path.isfile(self.path):
            try:
                with open(self.path, "r", encoding="utf-8") as fh:
                    self.data = json.load(fh)
            except (json.JSONDecodeError, OSError):
                pass
        self.data.setdefault("watermarks", {})

    def save(self) -> None:
        os.makedirs(os.path.dirname(os.path.abspath(self.path)) or ".", exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(self.data, fh, ensure_ascii=False, indent=2)
        os.replace(tmp, self.path)

    def get_watermark(self, table: str) -> str:
        return self.data["watermarks"].get(table, DEFAULT_WATERMARK)

    def set_watermark(self, table: str, value: str) -> None:
        self.data["watermarks"][table] = value

    def all_watermarks(self) -> dict:
        return dict(self.data["watermarks"])

    def mark_ok(self) -> None:
        self.data["last_sync_ok"] = datetime.now(timezone.utc).astimezone().isoformat(
            timespec="seconds")
