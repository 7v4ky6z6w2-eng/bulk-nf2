"""Registre des magasins (source unique de vérité).

`stores.json` décrit les 3 magasins : identité + connexion Firebird (IP Tailscale,
port, chemin de la base, identifiants, charset). Tous les composants (agent, hub,
application bureau) lisent ce fichier via :class:`StoreRegistry`.

La méthode :meth:`StoreRegistry.connect_kwargs` renvoie exactement le dictionnaire
que les outils primenf (BDR / éditeur) attendent déjà (``host/port/database/user/
password/charset``), afin de pouvoir exécuter le code existant sans modification
contre n'importe quel magasin.
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass, field
from typing import Optional


def app_dir() -> str:
    """Dossier de l'application (à côté de l'exe une fois packagé, sinon racine projet)."""
    if getattr(sys, "frozen", False):  # PyInstaller
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# Chemin par défaut : stores.json à côté de l'exe / à la racine du projet.
STORES_PATH = os.path.join(app_dir(), "stores.json")


class StoresError(Exception):
    """Erreur de lecture / configuration de stores.json."""


@dataclass
class Store:
    id: int
    name: str
    host: str = "localhost"
    port: int = 3050
    database: str = ""
    user: str = "SYSDBA"
    password: str = "masterkey"
    charset: str = "WIN1256"
    is_hub: bool = False

    def connect_kwargs(self, *, local: bool = False) -> dict:
        """kwargs pour ``fdb.connect`` (et pour les outils primenf).

        Si ``local`` est vrai, on force ``host=localhost`` : l'agent qui tourne
        SUR le poste du magasin accède à sa propre base en local, sans passer par
        le réseau Tailscale.
        """
        host = "localhost" if local else (self.host or "localhost")
        return {
            "host": host,
            "port": int(self.port or 3050),
            "database": self.database,
            "user": self.user or "SYSDBA",
            "password": self.password or "",
            "charset": self.charset or "WIN1256",
        }


@dataclass
class NotifyConfig:
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""
    ntfy_topic: str = ""

    @property
    def telegram_enabled(self) -> bool:
        return bool(self.telegram_bot_token and self.telegram_chat_id)

    @property
    def ntfy_enabled(self) -> bool:
        return bool(self.ntfy_topic)


@dataclass
class StoreRegistry:
    stores: list = field(default_factory=list)            # list[Store]
    notify: NotifyConfig = field(default_factory=NotifyConfig)
    hub_port: int = 5000
    hub_api_key: str = ""
    path: str = STORES_PATH

    def hub_url(self) -> str:
        """URL du serveur hub vue depuis CE poste (le host du magasin hub dans
        stores.json est localhost sur le hub, IP Tailscale sur les autres)."""
        return "http://%s:%d" % (self.hub().host or "localhost", self.hub_port)

    # --- chargement -------------------------------------------------------
    @classmethod
    def load(cls, path: str = STORES_PATH) -> "StoreRegistry":
        if not os.path.isfile(path):
            raise StoresError(
                "Fichier de configuration des magasins introuvable : %s\n"
                "Copiez stores.json.example en stores.json et remplissez-le." % path)
        try:
            with open(path, "r", encoding="utf-8") as fh:
                raw = json.load(fh)
        except json.JSONDecodeError as exc:
            raise StoresError("stores.json invalide (%s)." % exc) from exc

        stores = []
        for s in raw.get("stores", []):
            stores.append(Store(
                id=int(s["id"]),
                name=s.get("name", "Magasin %s" % s["id"]),
                host=s.get("host", "localhost"),
                port=int(s.get("port", 3050)),
                database=s.get("database", ""),
                user=s.get("user", "SYSDBA"),
                password=s.get("password", ""),
                charset=s.get("charset", "WIN1256"),
                is_hub=bool(s.get("is_hub", False)),
            ))
        if not stores:
            raise StoresError("Aucun magasin défini dans stores.json.")

        tg = raw.get("telegram", {}) or {}
        notify = NotifyConfig(
            telegram_bot_token=tg.get("bot_token", ""),
            telegram_chat_id=tg.get("chat_id", ""),
            ntfy_topic=raw.get("ntfy_topic", ""),
        )
        return cls(stores=stores, notify=notify,
                   hub_port=int(raw.get("hub_port", 5000)),
                   hub_api_key=raw.get("hub_api_key", ""),
                   path=path)

    # --- accès ------------------------------------------------------------
    def get(self, store_id: int) -> Store:
        for s in self.stores:
            if s.id == int(store_id):
                return s
        raise StoresError("Magasin id=%s absent de stores.json." % store_id)

    def connect_kwargs(self, store_id: int, *, local: bool = False) -> dict:
        return self.get(store_id).connect_kwargs(local=local)

    def hub(self) -> Store:
        for s in self.stores:
            if s.is_hub:
                return s
        return self.stores[0]

    def others(self) -> list:
        """Magasins non-hub (ceux qui poussent vers le hub)."""
        return [s for s in self.stores if not s.is_hub]

    def names(self) -> dict:
        return {s.id: s.name for s in self.stores}
