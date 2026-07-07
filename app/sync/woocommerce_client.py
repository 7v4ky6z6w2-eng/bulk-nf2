"""Thin WooCommerce REST API client.

Only implements what the sync engine needs: batch product create/update,
category find-or-create, and (separately, since it needs different auth)
uploading an image to the WordPress media library for ARTICLE.PHOTO blobs.
"""

import logging
import time

import requests

log = logging.getLogger(__name__)

BATCH_LIMIT = 100  # WooCommerce's hard cap per /products/batch call
MAX_RETRIES = 3
RETRY_BACKOFF_SECONDS = 2


class WooCommerceError(Exception):
    pass


class WooCommerceClient:
    def __init__(self, site_url, consumer_key, consumer_secret,
                 wp_username=None, wp_app_password=None, timeout=30):
        self.base_url = site_url.rstrip("/")
        self.auth = (consumer_key, consumer_secret)
        self.wp_auth = (wp_username, wp_app_password) if wp_username else None
        self.timeout = timeout
        self._category_cache = None

    def _request(self, method, path, auth, **kwargs):
        url = f"{self.base_url}{path}"
        last_exc = None
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                resp = requests.request(
                    method, url, auth=auth, timeout=self.timeout, **kwargs
                )
                if resp.status_code >= 500 and attempt < MAX_RETRIES:
                    time.sleep(RETRY_BACKOFF_SECONDS * attempt)
                    continue
                if resp.status_code >= 400:
                    raise WooCommerceError(
                        f"{method} {path} -> {resp.status_code}: {resp.text[:500]}"
                    )
                return resp.json() if resp.content else {}
            except requests.RequestException as exc:
                last_exc = exc
                if attempt < MAX_RETRIES:
                    time.sleep(RETRY_BACKOFF_SECONDS * attempt)
        raise WooCommerceError(f"{method} {path} failed after {MAX_RETRIES} attempts: {last_exc}")

    # -- products -----------------------------------------------------------
    def batch_products(self, create=None, update=None, delete=None, chunk_size=BATCH_LIMIT):
        """Runs /products/batch in chunks of <= chunk_size total items per
        call (capped at BATCH_LIMIT, WooCommerce's hard limit). Returns the
        combined 'create'/'update'/'delete' response lists."""
        chunk_size = min(chunk_size, BATCH_LIMIT)
        create = list(create or [])
        update = list(update or [])
        delete = list(delete or [])

        results = {"create": [], "update": [], "delete": []}
        while create or update or delete:
            chunk_create, create = create[:chunk_size], create[chunk_size:]
            remaining = chunk_size - len(chunk_create)
            chunk_update, update = update[:remaining], update[remaining:]
            remaining -= len(chunk_update)
            chunk_delete, delete = delete[:remaining], delete[remaining:]

            payload = {}
            if chunk_create:
                payload["create"] = chunk_create
            if chunk_update:
                payload["update"] = chunk_update
            if chunk_delete:
                payload["delete"] = chunk_delete

            resp = self._request(
                "POST", "/wp-json/wc/v3/products/batch",
                auth=self.auth, json=payload,
            )
            for key in ("create", "update", "delete"):
                results[key].extend(resp.get(key, []))
        return results

    def fetch_all_products(self, fields=("id", "sku", "stock_quantity", "manage_stock")):
        """Paginated fetch of every product, restricted to 'fields' (keeps
        the response small -- used by stock_sync to bulk-diff against the
        DB without pulling full product bodies). Explicitly requests
        status=any: WooCommerce's REST API defaults an un-filtered
        /products list to published items only, silently hiding anything
        sitting in draft/pending/private -- stock still needs to be kept
        correct on those too."""
        results = []
        page = 1
        while True:
            rows = self._request(
                "GET", "/wp-json/wc/v3/products",
                auth=self.auth,
                params={"per_page": 100, "page": page, "status": "any",
                        "_fields": ",".join(fields)},
            )
            if not rows:
                break
            results.extend(rows)
            if len(rows) < 100:
                break
            page += 1
        return results

    # -- categories -----------------------------------------------------------
    def _load_categories(self):
        if self._category_cache is not None:
            return
        self._category_cache = {}
        page = 1
        while True:
            rows = self._request(
                "GET", "/wp-json/wc/v3/products/categories",
                auth=self.auth, params={"per_page": 100, "page": page},
            )
            if not rows:
                break
            for row in rows:
                self._category_cache[row["name"].strip().lower()] = row["id"]
            if len(rows) < 100:
                break
            page += 1

    def find_or_create_category(self, name):
        if not name:
            return None
        self._load_categories()
        key = name.strip().lower()
        if key in self._category_cache:
            return self._category_cache[key]
        row = self._request(
            "POST", "/wp-json/wc/v3/products/categories",
            auth=self.auth, json={"name": name},
        )
        self._category_cache[key] = row["id"]
        return row["id"]

    # -- media (needs WordPress app-password auth, not the WC keys) ---------
    def upload_media(self, image_bytes, filename, mime_type):
        if not self.wp_auth:
            raise WooCommerceError(
                "Uploading an image requires WordPress username/app-password "
                "(the WooCommerce Consumer Key/Secret can't authenticate "
                "/wp-json/wp/v2/media)."
            )
        resp = self._request(
            "POST", "/wp-json/wp/v2/media",
            auth=self.wp_auth,
            headers={
                "Content-Disposition": f'attachment; filename="{filename}"',
                "Content-Type": mime_type,
            },
            data=image_bytes,
        )
        return resp["id"]
