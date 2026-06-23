"""Récupération de l'image produit depuis WooCommerce à partir du SKU (REF_ART).

API : GET {base_url}/wp-json/wc/v3/products?sku={sku}
Authentification : clé/secret en HTTP Basic (lecture seule conseillée).
Les images téléchargées sont mises en cache sur le disque.
"""

from __future__ import annotations

import os
from typing import Optional

import requests

from config import WooConfig, CACHE_DIR


class WooClient:
    def __init__(self, cfg: WooConfig, cache_dir: str = CACHE_DIR):
        self._cfg = cfg
        self._cache_dir = cache_dir
        os.makedirs(self._cache_dir, exist_ok=True)

    @property
    def configured(self) -> bool:
        c = self._cfg
        return bool(c.base_url and c.consumer_key and c.consumer_secret)

    def _cache_path(self, sku: str) -> str:
        safe = "".join(ch if ch.isalnum() else "_" for ch in sku)
        return os.path.join(self._cache_dir, f"{safe}.img")

    def get_image(self, sku: str, timeout: float = 6.0) -> Optional[str]:
        """Retourne le chemin local de l'image produit, ou None si indisponible.

        N'échoue jamais bruyamment : en cas d'erreur réseau ou d'absence
        d'image, retourne None pour que l'affichage prix/nom continue.
        """
        sku = (sku or "").strip()
        if not sku:
            return None

        cache_path = self._cache_path(sku)
        if os.path.exists(cache_path):
            return cache_path

        if not self.configured:
            return None

        try:
            url = self._cfg.base_url.rstrip("/") + "/wp-json/wc/v3/products"
            resp = requests.get(
                url,
                params={"sku": sku, "_fields": "images"},
                auth=(self._cfg.consumer_key, self._cfg.consumer_secret),
                timeout=timeout,
            )
            resp.raise_for_status()
            products = resp.json()
            if not products:
                return None
            images = products[0].get("images") or []
            if not images:
                return None
            img_src = images[0].get("src")
            if not img_src:
                return None

            img_resp = requests.get(img_src, timeout=timeout)
            img_resp.raise_for_status()
            with open(cache_path, "wb") as fh:
                fh.write(img_resp.content)
            return cache_path
        except Exception:
            return None

    def test(self, timeout: float = 6.0) -> None:
        """Teste l'accès à l'API WooCommerce (lève une exception en cas d'échec)."""
        if not self.configured:
            raise RuntimeError("WooCommerce non configuré")
        url = self._cfg.base_url.rstrip("/") + "/wp-json/wc/v3/products"
        resp = requests.get(
            url,
            params={"per_page": 1, "_fields": "id"},
            auth=(self._cfg.consumer_key, self._cfg.consumer_secret),
            timeout=timeout,
        )
        resp.raise_for_status()
