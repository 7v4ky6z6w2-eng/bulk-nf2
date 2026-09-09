"""Persistance de la configuration client (URL du hub + code d'accès).

Stockée dans un dossier utilisateur (pas à côté de l'exe, qui peut être en
lecture seule sous Program Files) :
  * Windows : %APPDATA%\\PrimeNF\\client.json
  * autres  : ~/.config/primenf/client.json
"""

from __future__ import annotations

import json
import os


def _config_dir() -> str:
    base = os.environ.get("APPDATA")
    if base:  # Windows
        return os.path.join(base, "PrimeNF")
    return os.path.join(os.path.expanduser("~"), ".config", "primenf")


CONFIG_PATH = os.path.join(_config_dir(), "client.json")


def load() -> dict:
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, json.JSONDecodeError):
        return {}


def save(hub_url: str, code: str) -> None:
    os.makedirs(_config_dir(), exist_ok=True)
    tmp = CONFIG_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump({"hub_url": hub_url, "code": code}, fh, ensure_ascii=False, indent=2)
    os.replace(tmp, CONFIG_PATH)


def clear() -> None:
    try:
        os.remove(CONFIG_PATH)
    except OSError:
        pass
