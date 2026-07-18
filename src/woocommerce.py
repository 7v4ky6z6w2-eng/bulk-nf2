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
        # Les photos passent par l'API publique « Store » : seule l'adresse du
        # site est nécessaire (clé/secret facultatifs).
        return bool(self._cfg.base_url)

    def _cache_path(self, sku: str) -> str:
        safe = "".join(ch if ch.isalnum() else "_" for ch in sku)
        return os.path.join(self._cache_dir, f"{safe}.img")

    def _store_products_url(self) -> str:
        return self._cfg.base_url.rstrip("/") + "/wp-json/wc/store/v1/products"

    def get_image(self, *skus: str, timeout: float = 8.0) -> Optional[str]:
        """Retourne le chemin local de l'image produit, ou None si indisponible.

        Essaie chaque identifiant fourni (réf. article, code-barres scanné…)
        comme SKU WooCommerce via l'API publique « Store » (aucune clé requise).
        N'échoue jamais bruyamment : renvoie None pour que l'affichage
        prix/nom continue.
        """
        # Candidats uniques, non vides.
        cands = []
        for s in skus:
            s = (s or "").strip()
            if s and s not in cands:
                cands.append(s)
        if not cands:
            _log("PHOTO: aucun identifiant -> pas de photo")
            return None

        cache_path = self._cache_path(cands[0])
        if os.path.exists(cache_path):
            _log(f"PHOTO: cache hit pour {cands[0]}")
            return cache_path

        if not self.configured:
            _log("PHOTO: adresse du site manquante -> pas de photo")
            return None

        url = self._store_products_url()
        for sku in cands:
            try:
                resp = requests.get(url, params={"sku": sku}, timeout=timeout)
                _log(f"PHOTO: GET {url}?sku={sku} -> HTTP {resp.status_code}")
                resp.raise_for_status()
                products = resp.json()
                if not products:
                    _log(f"PHOTO: aucun produit avec SKU={sku}")
                    continue
                images = products[0].get("images") or []
                if not images:
                    _log(f"PHOTO: produit SKU={sku} sans image")
                    continue
                img_src = images[0].get("src")
                if not img_src:
                    continue
                img_resp = requests.get(img_src, timeout=timeout)
                _log(f"PHOTO: téléchargement {img_src[:80]} -> HTTP {img_resp.status_code}")
                img_resp.raise_for_status()
                with open(cache_path, "wb") as fh:
                    fh.write(img_resp.content)
                _log(f"PHOTO: OK (SKU={sku}) -> {cache_path}")
                return cache_path
            except Exception as exc:  # noqa: BLE001
                _log(f"PHOTO: ERREUR SKU={sku} : {exc!r}")
                continue

        _log(f"PHOTO: aucune photo trouvée (essayés : {', '.join(cands)})")
        return None

    def test(self, timeout: float = 10.0) -> None:
        """Teste l'accès à l'API publique WooCommerce (lève si échec)."""
        if not self.configured:
            raise RuntimeError("Adresse du site manquante")
        resp = requests.get(self._store_products_url(),
                            params={"per_page": 1}, timeout=timeout)
        resp.raise_for_status()
