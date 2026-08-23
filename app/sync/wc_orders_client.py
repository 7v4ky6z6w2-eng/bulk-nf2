"""WooCommerce orders client.

Deliberately separate from the main WooCommerceClient
(app/sync/woocommerce_client.py) because the user's real site was found
(via their existing, working wc_order_import.py) to need two workarounds
that the product-sync endpoints don't:

  1. Query-string auth (consumer_key/consumer_secret as URL params)
     instead of HTTP Basic Auth -- this host strips the Authorization
     header specifically on the orders endpoint.
  2. A browser-like User-Agent -- this host's WAF blocks the default
     python-requests UA.

Both are real, already-debugged fixes for this exact site, kept as-is
rather than rediscovering them.
"""

import time

import requests

RETRY_STATUSES = (408, 429, 500, 502, 503, 504, 520, 521, 522, 524)
MAX_ATTEMPTS = 5
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36"
)


class WCOrdersClient:
    def __init__(self, site_url, consumer_key, consumer_secret):
        self.base = site_url.rstrip("/")
        self.key = consumer_key
        self.secret = consumer_secret
        self._var_cache = {}
        self._prod_cache = {}

    def _url(self, path):
        return f"{self.base}/wp-json/wc/v3/{path.lstrip('/')}"

    def _request(self, method, url, **kwargs):
        timeout = kwargs.pop("timeout", 30)
        params = kwargs.pop("params", None) or {}
        params["consumer_key"] = self.key
        params["consumer_secret"] = self.secret
        kwargs["params"] = params
        headers = kwargs.pop("headers", None) or {}
        headers.setdefault("User-Agent", USER_AGENT)
        headers.setdefault("Accept", "application/json")
        kwargs["headers"] = headers

        last_exc = None
        for attempt in range(1, MAX_ATTEMPTS + 1):
            try:
                resp = requests.request(method, url, timeout=timeout, **kwargs)
                if resp.status_code in RETRY_STATUSES and attempt < MAX_ATTEMPTS:
                    time.sleep(5 * attempt)
                    continue
                return resp
            except (requests.exceptions.SSLError, requests.exceptions.ConnectionError,
                    requests.exceptions.Timeout, requests.exceptions.ChunkedEncodingError) as exc:
                last_exc = exc
                if attempt < MAX_ATTEMPTS:
                    time.sleep(5 * attempt)
        raise RuntimeError(f"{method} {url} failed after {MAX_ATTEMPTS} attempts: {last_exc}")

    def get_variation_sku(self, product_id, variation_id):
        """Resolve SKU for a WC product variation (line items don't always
        carry SKU). Cached after first lookup."""
        key = (product_id, variation_id)
        if key in self._var_cache:
            return self._var_cache[key]
        sku = ""
        resp = self._request("GET", self._url(f"products/{product_id}/variations/{variation_id}"))
        if resp.status_code == 200:
            sku = (resp.json().get("sku") or "").strip()
        self._var_cache[key] = sku
        return sku

    def get_product_sku(self, product_id):
        """Resolve SKU for a simple WC product. Cached."""
        if product_id in self._prod_cache:
            return self._prod_cache[product_id]
        sku = ""
        resp = self._request("GET", self._url(f"products/{product_id}"))
        if resp.status_code == 200:
            sku = (resp.json().get("sku") or "").strip()
        self._prod_cache[product_id] = sku
        return sku

    def fetch_orders(self, statuses, after=None):
        """'statuses' is an iterable of wanted WC order statuses. Status
        filtering happens client-side (not via the 'status' query param) --
        this host's security layer was found to 401 requests that filter
        by status server-side."""
        wanted = {s.strip().lower() for s in statuses if s.strip()}
        results = []
        seen_ids = set()
        page = 1
        per_page = 50
        while True:
            params = {"per_page": per_page, "page": page}
            if after:
                params["after"] = after
            resp = self._request("GET", self._url("orders"), params=params)
            if resp.status_code != 200:
                raise RuntimeError(
                    f"WC orders fetch failed (page={page}): {resp.status_code} {resp.text[:200]}"
                )
            items = resp.json()
            if not items:
                break
            for order in items:
                if order["id"] in seen_ids:
                    continue
                seen_ids.add(order["id"])
                if (order.get("status") or "").lower() in wanted:
                    results.append(order)
            if len(items) < per_page:
                break
            page += 1
        results.sort(key=lambda o: o.get("date_created", ""))
        return results

    def fetch_all_orders(self, after=None):
        """Every order regardless of status (unlike fetch_orders(), which
        only keeps a specific wanted set) -- used by the Yalidine
        reconciliation tool, which needs to see every order that might
        have been dispatched to a courier, not just ones in a particular
        WooCommerce status."""
        results = []
        seen_ids = set()
        page = 1
        per_page = 50
        while True:
            params = {"per_page": per_page, "page": page}
            if after:
                params["after"] = after
            resp = self._request("GET", self._url("orders"), params=params)
            if resp.status_code != 200:
                raise RuntimeError(
                    f"WC orders fetch failed (page={page}): {resp.status_code} {resp.text[:200]}"
                )
            items = resp.json()
            if not items:
                break
            for order in items:
                if order["id"] not in seen_ids:
                    seen_ids.add(order["id"])
                    results.append(order)
            if len(items) < per_page:
                break
            page += 1
        results.sort(key=lambda o: o.get("date_created", ""))
        return results
