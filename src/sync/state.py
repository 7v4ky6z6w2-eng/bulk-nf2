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
MAX_APPLIED_OPS = 2000  # borne la taille du fichier (purge des plus anciens)

# Version du schéma de synchro. À chaque fois qu'une table incrémentale gagne
# de NOUVELLES colonnes (ex. promos sur article), les lignes déjà synchronisées
# ne seraient jamais re-poussées (leur DATEMODIF n'a pas bougé) : on liste ici
# les watermarks à réinitialiser pour forcer UNE resynchronisation complète de
# ces tables au premier cycle après mise à jour de l'agent.
STATE_VERSION = 2
_RESYNC_ON_UPGRADE = {
    2: ["article"],   # v2 : colonnes promo ajoutées au miroir article
}


class StateManager:
    def __init__(self, path: str):
        self.path = path
        self.data = {"store_id": None, "watermarks": {}, "last_sync_ok": None,
                     "applied_ops": [], "version": STATE_VERSION}
        self.load()

    def load(self) -> None:
        if os.path.isfile(self.path):
            try:
                with open(self.path, "r", encoding="utf-8") as fh:
                    self.data = json.load(fh)
            except (json.JSONDecodeError, OSError):
                pass
        self.data.setdefault("watermarks", {})
        self.data.setdefault("applied_ops", [])
        old = int(self.data.get("version") or 1)
        if old < STATE_VERSION:
            for v in range(old + 1, STATE_VERSION + 1):
                for table in _RESYNC_ON_UPGRADE.get(v, []):
                    self.data["watermarks"].pop(table, None)
            self.data["version"] = STATE_VERSION

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

    # -- suivi des opérations déjà appliquées (idempotence) -----------------
    # Si l'acquittement au hub échoue après une écriture Firebird réussie (op
    # écrite mais toujours 'pending' côté hub), le prochain cycle la
    # re-proposera. Ce registre local évite de la ré-exécuter : on se contente
    # alors de ré-acquitter.
    def is_op_applied(self, op_id: int) -> bool:
        return op_id in self.data["applied_ops"]

    def mark_op_applied(self, op_id: int) -> None:
        if op_id not in self.data["applied_ops"]:
            self.data["applied_ops"].append(op_id)
            if len(self.data["applied_ops"]) > MAX_APPLIED_OPS:
                self.data["applied_ops"] = self.data["applied_ops"][-MAX_APPLIED_OPS:]
        self.save()
