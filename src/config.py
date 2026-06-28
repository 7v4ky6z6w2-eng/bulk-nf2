"""Chargement et sauvegarde de la configuration (config.ini).

Le fichier de configuration est placé à côté de l'exécutable. Il est créé /
modifié par l'assistant de première configuration (ui/setup_window.py).
"""

from __future__ import annotations

import base64
import configparser
import os
import sys
from dataclasses import dataclass, field


# --- Localisation du fichier de configuration -----------------------------

def app_dir() -> str:
    """Dossier de l'application (à côté de l'exe une fois packagé, sinon racine du projet)."""
    if getattr(sys, "frozen", False):  # PyInstaller
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


CONFIG_PATH = os.path.join(app_dir(), "config.ini")
CACHE_DIR = os.path.join(app_dir(), "cache")


# --- Obfuscation simple du mot de passe -----------------------------------
# Ce n'est PAS un chiffrement fort : cela évite seulement d'écrire le mot de
# passe en clair dans config.ini. Le mot de passe SYSDBA reste sensible.
_OBF_PREFIX = "enc:"
_OBF_KEY = b"netfact-affichage-prix"


def _xor(data: bytes) -> bytes:
    return bytes(b ^ _OBF_KEY[i % len(_OBF_KEY)] for i, b in enumerate(data))


def obfuscate(value: str) -> str:
    if not value:
        return ""
    token = base64.b64encode(_xor(value.encode("utf-8"))).decode("ascii")
    return _OBF_PREFIX + token


def deobfuscate(value: str) -> str:
    if not value:
        return ""
    if not value.startswith(_OBF_PREFIX):
        return value  # ancien format / saisi à la main
    raw = base64.b64decode(value[len(_OBF_PREFIX):].encode("ascii"))
    return _xor(raw).decode("utf-8")


# --- Structure de configuration -------------------------------------------

@dataclass
class FirebirdConfig:
    host: str = ""
    port: int = 3050
    database: str = ""
    user: str = "SYSDBA"
    password: str = ""
    charset: str = "WIN1256"


@dataclass
class SyncConfig:
    store_id: int = 0
    store_name: str = ""
    hub_url: str = ""
    hub_api_key: str = ""
    interval_minutes: int = 15
    state_file: str = ""


@dataclass
class HubConfig:
    store_id: int = 1
    store_name: str = ""
    db_path: str = ""
    flask_port: int = 5000
    secret_key: str = ""
    api_keys: dict = field(default_factory=dict)   # {store_id: api_key}
    store_names: dict = field(default_factory=dict)  # {store_id: name}


@dataclass
class WooConfig:
    base_url: str = ""
    consumer_key: str = ""
    consumer_secret: str = ""


@dataclass
class UiConfig:
    idle_reset_seconds: int = 8
    fullscreen: bool = True
    exit_hotkey: str = "Ctrl+Alt+Q"


@dataclass
class AppConfig:
    firebird: FirebirdConfig = field(default_factory=FirebirdConfig)
    woocommerce: WooConfig = field(default_factory=WooConfig)
    ui: UiConfig = field(default_factory=UiConfig)

    def is_complete(self) -> bool:
        """Vrai si les informations minimales de connexion à la base sont présentes."""
        fb = self.firebird
        return bool(fb.database and fb.user and fb.password)


# --- Lecture / écriture ----------------------------------------------------

def load(path: str = CONFIG_PATH) -> AppConfig:
    cfg = AppConfig()
    if not os.path.exists(path):
        return cfg

    parser = configparser.ConfigParser()
    parser.read(path, encoding="utf-8")

    if parser.has_section("firebird"):
        s = parser["firebird"]
        cfg.firebird = FirebirdConfig(
            host=s.get("host", ""),
            port=s.getint("port", 3050),
            database=s.get("database", ""),
            user=s.get("user", "SYSDBA"),
            password=deobfuscate(s.get("password", "")),
        )
    if parser.has_section("woocommerce"):
        s = parser["woocommerce"]
        cfg.woocommerce = WooConfig(
            base_url=s.get("base_url", "").rstrip("/"),
            consumer_key=s.get("consumer_key", ""),
            consumer_secret=s.get("consumer_secret", ""),
        )
    if parser.has_section("ui"):
        s = parser["ui"]
        cfg.ui = UiConfig(
            idle_reset_seconds=s.getint("idle_reset_seconds", 8),
            fullscreen=s.getboolean("fullscreen", True),
            exit_hotkey=s.get("exit_hotkey", "Ctrl+Alt+Q"),
        )
    return cfg


def save(cfg: AppConfig, path: str = CONFIG_PATH) -> None:
    parser = configparser.ConfigParser()
    parser["firebird"] = {
        "host": cfg.firebird.host,
        "port": str(cfg.firebird.port),
        "database": cfg.firebird.database,
        "user": cfg.firebird.user,
        "password": obfuscate(cfg.firebird.password),
    }
    parser["woocommerce"] = {
        "base_url": cfg.woocommerce.base_url.rstrip("/"),
        "consumer_key": cfg.woocommerce.consumer_key,
        "consumer_secret": cfg.woocommerce.consumer_secret,
    }
    parser["ui"] = {
        "idle_reset_seconds": str(cfg.ui.idle_reset_seconds),
        "fullscreen": str(cfg.ui.fullscreen).lower(),
        "exit_hotkey": cfg.ui.exit_hotkey,
    }
    with open(path, "w", encoding="utf-8") as fh:
        parser.write(fh)
