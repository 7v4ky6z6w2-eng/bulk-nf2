"""Load/save the tool's JSON configuration file.

Mirrors the config-with-defaults pattern used by the existing
import_bon_reception.py tool (deep-merge a user JSON file over defaults),
so anyone already familiar with that script's config style will recognize
this one.
"""

import copy
import json
import os

DEFAULT_CONFIG = {
    # --- Firebird connection (same shape as import_bon_reception.py) -------
    "firebird": {
        "host": "",                       # "" = local direct file access
        "port": 3050,
        "database": "C:\\PRIME\\PR22.FDB",  # path to the .FDB file
        "user": "SYSDBA",
        "password": "masterkey",
        "charset": "WIN1256",
        "fb_client_library": "",          # optional explicit path to fbclient.dll
    },

    # --- Sync scope / field mapping -----------------------------------------
    "sync": {
        # Only sync articles whose family is flagged visible in the web shop.
        # Set to false if BOUTIQ_VISIBLE isn't actually used in this install.
        "filter_boutique_visible": True,
        # Which sale-price field to use as WooCommerce's regular_price:
        # "PRIXVENTETTC" (tax-inclusive) or "PRIXVENTEHT" (tax-exclusive) —
        # depends on the store's "prices entered with tax" setting.
        "price_field": "PRIXVENTETTC",
        "promo_price_field": "PRIXTTCPROMO",
        "sync_images": True,
        # Abbreviation -> full word, applied (case-insensitively) before
        # Title-casing the product name. Seeded from the user's own
        # real-world list (sync_config.ini's [replacements] section).
        "name_replacements": {
            "GM": "Grand Modèle",
            "PM": "Petit Modèle",
            "MM": "Moyen Modèle",
            "STYL": "Stylo",
            "CAH": "Cahier",
            "ARB": "Ardoise",
            "PQT": "Paquet",
            "RECH": "Recharge",
            "CLAS": "Classeur",
            "BIL": "Bille",
            "PCH": "Pochette",
            "BTE": "Boîte",
            "FT": "Format",
            "COUL": "Couleur",
            "NR": "Noir",
            "BLC": "Blanc",
            "ROU": "Rouge",
            "BLU": "Bleu",
        },
    },

    # --- WooCommerce REST API -----------------------------------------------
    "woocommerce": {
        "site_url": "",                   # e.g. https://example.com
        "consumer_key": "",
        "consumer_secret": "",
    },

    # --- WordPress (only needed for uploading ARTICLE.PHOTO blobs) ---------
    "wordpress": {
        "username": "",
        "app_password": "",
    },

    # --- Local state ----------------------------------------------------------
    "state_db_path": "sync_state.sqlite3",

    # --- Scheduling -----------------------------------------------------------
    "schedule": {
        "enabled": False,
        "interval_hours": 6,
    },
}


def _deep_merge(base, overrides):
    for key, value in overrides.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            _deep_merge(base[key], value)
        else:
            base[key] = value
    return base


def load_config(path):
    """Load config from 'path', deep-merged over DEFAULT_CONFIG.

    If 'path' doesn't exist yet, returns a copy of the defaults (the caller
    is expected to save it once the user fills in real values via the GUI).
    """
    cfg = copy.deepcopy(DEFAULT_CONFIG)
    if path and os.path.isfile(path):
        with open(path, "r", encoding="utf-8") as fh:
            user_cfg = json.load(fh)
        _deep_merge(cfg, user_cfg)
    return cfg


def save_config(path, cfg):
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(cfg, fh, ensure_ascii=False, indent=2)
