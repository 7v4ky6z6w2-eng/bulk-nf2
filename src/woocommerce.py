"""Récupération de l'image produit depuis WooCommerce à partir du SKU (REF_ART).

API : GET {base_url}/wp-json/wc/v3/products?sku={sku}
Authentification : clé/secret en HTTP Basic (lecture seule conseillée).
Les images téléchargées sont mises en cache sur le disque.
"""

from __future__ import annotations

import os
from typing import Optional

import requests

from config import WooConfig, CACHE_DIR, log as _log


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
            _log("PHOTO: SKU vide -> pas de photo")
            return None

        cache_path = self._cache_path(sku)
        if os.path.exists(cache_path):
            _log(f"PHOTO: cache hit pour SKU={sku}")
            return cache_path

        if not self.configured:
            _log("PHOTO: WooCommerce non configuré -> pas de photo")
            return None

        try:
            url = self._cfg.base_url.rstrip("/") + "/wp-json/wc/v3/products"
            resp = requests.get(
                url,
                params={"sku": sku, "_fields": "images"},
                auth=(self._cfg.consumer_key, self._cfg.consumer_secret),
                timeout=timeout,
            )
            _log(f"PHOTO: GET {url}?sku={sku} -> HTTP {resp.status_code}")
            resp.raise_for_status()
            products = resp.json()
            if not products:
                _log(f"PHOTO: aucun produit avec SKU={sku} (vérifier que "
                     f"REF_ART == SKU WooCommerce)")
                return None
            images = products[0].get("images") or []
            if not images:
                _log(f"PHOTO: produit SKU={sku} trouvé mais sans image")
                return None
            img_src = images[0].get("src")
            if not img_src:
                _log(f"PHOTO: image sans URL src pour SKU={sku}")
                return None

            img_resp = requests.get(img_src, timeout=timeout)
            _log(f"PHOTO: téléchargement {img_src} -> HTTP {img_resp.status_code}")
            img_resp.raise_for_status()
            with open(cache_path, "wb") as fh:
                fh.write(img_resp.content)
            _log(f"PHOTO: OK, enregistrée dans {cache_path}")
            return cache_path
        except Exception as exc:  # noqa: BLE001
            _log(f"PHOTO: ERREUR pour SKU={sku} : {exc!r}")
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
