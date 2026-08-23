"""Thin WooCommerce REST API client.

Only implements what the sync engine needs: batch product create/update,
and (separately, since it needs different auth) uploading an image to the
WordPress media library for ARTICLE.PHOTO blobs. Categories are
deliberately not touched here -- the store runs its own WordPress
auto-categorizer plugin instead.
"""

import logging
import time

import requests

log = logging.getLogger(__name__)

BATCH_LIMIT = 100  # WooCommerce's hard cap per /products/batch call
MAX_RETRIES = 3
RETRY_BACKOFF_SECONDS = 2
# Creating/updating up to 100 full products (category assignment, meta,
# other plugins' save_post hooks) can legitimately take much longer than a
# simple GET, especially on shared hosting -- give batch calls more room
# than the default request timeout before giving up.
BATCH_TIMEOUT_SECONDS = 120


class WooCommerceError(Exception):
    pass


class WooCommerceClient:
    def __init__(self, site_url, consumer_key, consumer_secret,
                 wp_username=None, wp_app_password=None, timeout=30):
        self.base_url = site_url.rstrip("/")
        self.auth = (consumer_key, consumer_secret)
        self.wp_auth = (wp_username, wp_app_password) if wp_username else None
        self.timeout = timeout

    def _request(self, method, path, auth, timeout=None, **kwargs):
        url = f"{self.base_url}{path}"
        last_exc = None
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                resp = requests.request(
                    method, url, auth=auth, timeout=timeout or self.timeout, **kwargs
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
    def batch_products(self, create=None, update=None, delete=None,
                        chunk_size=BATCH_LIMIT, progress_fn=None):
        """Runs /products/batch in chunks of <= chunk_size total items per
        call (capped at BATCH_LIMIT, WooCommerce's hard limit). Returns the
        combined 'create'/'update'/'delete' response lists.

        A chunk that fails outright (timeout, network error, 4xx/5xx after
        retries) does NOT abort the run: its items are recorded as errors
        in the returned results (same {"sku", "error"} shape a per-item WC
        validation error has), and the next chunk is still attempted. A
        large sync shouldn't lose every already-succeeded chunk because one
        transient failure happened partway through.

        'progress_fn(chunk_num, total_chunks, item_count)' is called right
        before each chunk is sent, if provided -- lets the caller show
        progress during what can otherwise be several silent minutes."""
        chunk_size = min(chunk_size, BATCH_LIMIT)
        create = list(create or [])
        update = list(update or [])
        delete = list(delete or [])

        total_items = len(create) + len(update) + len(delete)
        total_chunks = -(-total_items // chunk_size) if total_items else 0

        results = {"create": [], "update": [], "delete": []}
        chunk_num = 0
        while create or update or delete:
            chunk_num += 1
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

            if progress_fn:
                progress_fn(chunk_num, total_chunks,
                            len(chunk_create) + len(chunk_update) + len(chunk_delete))

            try:
                resp = self._request(
                    "POST", "/wp-json/wc/v3/products/batch",
                    auth=self.auth, json=payload, timeout=BATCH_TIMEOUT_SECONDS,
                )
                for key in ("create", "update", "delete"):
                    results[key].extend(resp.get(key, []))
            except WooCommerceError as exc:
                log.error("Batch chunk %d/%d failed, marking %d item(s) as errored: %s",
                          chunk_num, total_chunks,
                          len(chunk_create) + len(chunk_update) + len(chunk_delete), exc)
                for key, chunk in (("create", chunk_create), ("update", chunk_update)):
                    for item in chunk:
                        results[key].append({"sku": item.get("sku"), "error": str(exc)})
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

    def ping(self):
        """Lightweight connectivity/auth check for the GUI's Test Connection
        button: fetch a single product. Raises WooCommerceError on failure
        (bad URL, bad key/secret, network issue, etc.)."""
        self._request(
            "GET", "/wp-json/wc/v3/products",
            auth=self.auth, params={"per_page": 1, "_fields": "id"},
        )

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
