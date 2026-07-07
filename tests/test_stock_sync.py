import copy

from app.config import DEFAULT_CONFIG
from app.sync import stock_sync


def _cfg(**overrides):
    cfg = copy.deepcopy(DEFAULT_CONFIG)
    cfg["woocommerce"]["site_url"] = "https://example.com"
    cfg["woocommerce"]["consumer_key"] = "ck"
    cfg["woocommerce"]["consumer_secret"] = "cs"
    cfg["stock_sync"].update(overrides)
    return cfg


class DummyConnection:
    def close(self):
        pass


class FakeWooCommerceClient:
    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.batch_calls = []
        self.products = []  # set by the test before run_stock_sync is called

    def fetch_all_products(self, fields=None):
        return self.products

    def batch_products(self, create=None, update=None, delete=None, chunk_size=100):
        self.batch_calls.append(update or [])
        return {"create": [], "update": [{"id": u["id"]} for u in (update or [])], "delete": []}


def _patch(monkeypatch, db_stock, products):
    fake_client = FakeWooCommerceClient()
    fake_client.products = products
    monkeypatch.setattr(stock_sync, "connect_firebird", lambda cfg: DummyConnection())
    monkeypatch.setattr(stock_sync.queries, "fetch_stock_quantities", lambda con: db_stock)
    monkeypatch.setattr(stock_sync, "WooCommerceClient", lambda **kwargs: fake_client)
    return fake_client


def test_updates_product_with_changed_stock(monkeypatch):
    client = _patch(
        monkeypatch,
        db_stock={"REF1": 10},
        products=[{"id": 1, "sku": "REF1", "stock_quantity": 5, "manage_stock": True}],
    )
    report = stock_sync.run_stock_sync(_cfg())
    assert report["updated"] == ["REF1"]
    assert client.batch_calls == [[{"id": 1, "manage_stock": True, "stock_quantity": 10}]]


def test_skips_unchanged_product(monkeypatch):
    client = _patch(
        monkeypatch,
        db_stock={"REF1": 10},
        products=[{"id": 1, "sku": "REF1", "stock_quantity": 10, "manage_stock": True}],
    )
    report = stock_sync.run_stock_sync(_cfg())
    assert report["updated"] == []
    assert report["unchanged"] == 1
    assert client.batch_calls == []


def test_enables_manage_stock_even_if_quantity_matches(monkeypatch):
    # manage_stock currently False -- must still be turned on even though
    # the raw quantity number happens to already match.
    _patch(
        monkeypatch,
        db_stock={"REF1": 10},
        products=[{"id": 1, "sku": "REF1", "stock_quantity": 10, "manage_stock": False}],
    )
    report = stock_sync.run_stock_sync(_cfg())
    assert report["updated"] == ["REF1"]


def test_counts_products_without_sku(monkeypatch):
    _patch(
        monkeypatch,
        db_stock={},
        products=[{"id": 1, "sku": "", "stock_quantity": 0, "manage_stock": False}],
    )
    report = stock_sync.run_stock_sync(_cfg())
    assert report["no_sku"] == 1
    assert report["updated"] == []


def test_missing_in_db_not_zeroed_by_default(monkeypatch):
    client = _patch(
        monkeypatch,
        db_stock={},
        products=[{"id": 1, "sku": "REF1", "stock_quantity": 5, "manage_stock": True}],
    )
    report = stock_sync.run_stock_sync(_cfg(zero_missing_in_db=False))
    assert report["missing_in_db"] == 1
    assert report["updated"] == []
    assert client.batch_calls == []


def test_missing_in_db_zeroed_when_enabled(monkeypatch):
    client = _patch(
        monkeypatch,
        db_stock={},
        products=[{"id": 1, "sku": "REF1", "stock_quantity": 5, "manage_stock": True}],
    )
    report = stock_sync.run_stock_sync(_cfg(zero_missing_in_db=True))
    assert report["missing_in_db"] == 1
    assert report["updated"] == ["REF1"]
    assert client.batch_calls == [[{"id": 1, "manage_stock": True, "stock_quantity": 0}]]


def test_dry_run_does_not_call_batch_update(monkeypatch):
    client = _patch(
        monkeypatch,
        db_stock={"REF1": 10},
        products=[{"id": 1, "sku": "REF1", "stock_quantity": 5, "manage_stock": True}],
    )
    report = stock_sync.run_stock_sync(_cfg(), dry_run=True)
    assert client.batch_calls == []
    assert len(report["updates_preview"]) == 1
    assert report["updates_preview"][0]["sku"] == "REF1"
