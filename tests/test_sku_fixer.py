import copy

from app.config import DEFAULT_CONFIG
from app.sync import sku_fixer


class FakeCursor:
    def __init__(self, existing_refs):
        self.existing_refs = existing_refs
        self._last = []

    def execute(self, sql, params=None):
        # WHERE REF_ART IN (...) lookup -- params is the list of refs asked for.
        self._last = [(r,) for r in params if r in self.existing_refs]

    def fetchall(self):
        return self._last


class FakeConnection:
    def __init__(self, cur):
        self._cur = cur

    def cursor(self):
        return self._cur

    def close(self):
        pass


def _cfg(**overrides):
    cfg = copy.deepcopy(DEFAULT_CONFIG)
    cfg["woocommerce"]["site_url"] = "https://example.com"
    cfg["woocommerce"]["consumer_key"] = "ck"
    cfg["woocommerce"]["consumer_secret"] = "cs"
    cfg.update(overrides)
    return cfg


class FakeWooCommerceClient:
    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.batch_calls = []
        self.products = []
        self.batch_result = None

    def fetch_all_products(self, fields=None):
        return self.products

    def batch_products(self, create=None, update=None, delete=None, chunk_size=100, progress_fn=None):
        self.batch_calls.append(update or [])
        if self.batch_result is not None:
            return self.batch_result
        return {"create": [], "update": [{"id": u["id"]} for u in (update or [])], "delete": []}


def _connect(monkeypatch, existing_refs=(), client=None):
    cur = FakeCursor(set(existing_refs))
    con = FakeConnection(cur)
    monkeypatch.setattr(sku_fixer, "connect_firebird", lambda cfg: con)
    client = client or FakeWooCommerceClient()
    monkeypatch.setattr(sku_fixer, "WooCommerceClient", lambda **kwargs: client)
    return client


def test_list_products_missing_sku_filters_correctly(monkeypatch):
    client = _connect(monkeypatch)
    client.products = [
        {"id": 1, "sku": "REF1", "name": "Has SKU"},
        {"id": 2, "sku": "", "name": "No SKU"},
        {"id": 3, "sku": None, "name": "Also no SKU"},
        {"id": 4, "sku": "  ", "name": "Blank SKU"},
    ]
    result = sku_fixer.list_products_missing_sku(_cfg())
    assert {r["id"] for r in result} == {2, 3, 4}


def test_apply_sku_fixes_dry_run_does_not_call_batch_update(monkeypatch):
    client = _connect(monkeypatch, existing_refs={"REF1"})
    report = sku_fixer.apply_sku_fixes(
        _cfg(), [{"product_id": 10, "ref_art": "REF1"}], dry_run=True
    )
    assert client.batch_calls == []
    assert report["applied"] == [{"product_id": 10, "ref_art": "REF1"}]
    assert report["invalid_ref"] == []


def test_apply_sku_fixes_skips_invalid_ref_art(monkeypatch):
    client = _connect(monkeypatch, existing_refs={"REF1"})
    report = sku_fixer.apply_sku_fixes(
        _cfg(), [{"product_id": 10, "ref_art": "REF1"},
                 {"product_id": 11, "ref_art": "TYPO_REF"}],
    )
    assert report["invalid_ref"] == [{"product_id": 11, "ref_art": "TYPO_REF"}]
    assert client.batch_calls == [[{"id": 10, "sku": "REF1"}]]  # TYPO_REF never sent
    assert report["applied"] == [{"product_id": 10, "ref_art": "REF1"}]


def test_apply_sku_fixes_reports_wc_side_errors(monkeypatch):
    client = _connect(monkeypatch, existing_refs={"REF1", "REF2"})
    client.batch_result = {
        "create": [], "delete": [],
        "update": [{"id": 10, "sku": "REF1"}, {"id": 11, "error": "sku already in use"}],
    }
    report = sku_fixer.apply_sku_fixes(
        _cfg(), [{"product_id": 10, "ref_art": "REF1"},
                 {"product_id": 11, "ref_art": "REF2"}],
    )
    assert report["applied"] == [{"product_id": 10, "ref_art": "REF1"}]
    assert report["errors"] == [{"product_id": 11, "ref_art": "REF2", "error": "sku already in use"}]


def test_apply_sku_fixes_with_blank_entries_ignored(monkeypatch):
    client = _connect(monkeypatch, existing_refs={"REF1"})
    report = sku_fixer.apply_sku_fixes(
        _cfg(), [{"product_id": 10, "ref_art": ""}, {"product_id": 11, "ref_art": "  "}],
    )
    assert report == {"applied": [], "invalid_ref": [], "errors": []}
    assert client.batch_calls == []
