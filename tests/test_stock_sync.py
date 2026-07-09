import copy
import os
import tempfile

from app.config import DEFAULT_CONFIG
from app.sync import stock_sync
from app.sync.state_store import StateStore


def _cfg(state_path, **overrides):
    cfg = copy.deepcopy(DEFAULT_CONFIG)
    cfg["state_db_path"] = state_path
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

    def batch_products(self, create=None, update=None, delete=None, chunk_size=100, progress_fn=None):
        self.batch_calls.append(update or [])
        return {"create": [], "update": [{"id": u["id"]} for u in (update or [])], "delete": []}


def _setup(monkeypatch, tmp, db_stock, adopted):
    """adopted: dict {ref_art: (wc_product_id, last_stock)} seeded into the store."""
    state_path = os.path.join(tmp, "state.sqlite3")
    with StateStore(state_path) as store:
        for ref_art, (wc_id, last_stock) in adopted.items():
            store.adopt(ref_art, wc_id, "2026-01-01T00:00:00")
            if last_stock is not None:
                store.set_last_stocks([(ref_art, last_stock)], "2026-01-01T00:00:00")

    client = FakeWooCommerceClient()
    monkeypatch.setattr(stock_sync, "connect_firebird", lambda cfg: DummyConnection())
    monkeypatch.setattr(stock_sync.queries, "fetch_stock_quantities", lambda con: db_stock)
    monkeypatch.setattr(stock_sync, "WooCommerceClient", lambda **kwargs: client)
    return state_path, client


def test_updates_product_with_changed_stock(monkeypatch):
    with tempfile.TemporaryDirectory() as tmp:
        state_path, client = _setup(
            monkeypatch, tmp, db_stock={"REF1": 10}, adopted={"REF1": (1, 5)},
        )
        report = stock_sync.run_stock_sync(_cfg(state_path))
        assert report["updated"] == ["REF1"]
        assert client.batch_calls == [[{"id": 1, "manage_stock": True, "stock_quantity": 10}]]
        # last_stock persisted so a second run is a no-op
        with StateStore(state_path) as store:
            assert store.get_stock_targets()["REF1"] == (1, 10)


def test_skips_unchanged_product(monkeypatch):
    with tempfile.TemporaryDirectory() as tmp:
        state_path, client = _setup(
            monkeypatch, tmp, db_stock={"REF1": 10}, adopted={"REF1": (1, 10)},
        )
        report = stock_sync.run_stock_sync(_cfg(state_path))
        assert report["updated"] == []
        assert report["unchanged"] == 1
        assert client.batch_calls == []


def test_first_sync_pushes_when_last_stock_unknown(monkeypatch):
    with tempfile.TemporaryDirectory() as tmp:
        # Freshly adopted product: last_stock is NULL -> must push once.
        state_path, client = _setup(
            monkeypatch, tmp, db_stock={"REF1": 7}, adopted={"REF1": (1, None)},
        )
        report = stock_sync.run_stock_sync(_cfg(state_path))
        assert report["updated"] == ["REF1"]


def test_article_not_tracked_is_counted_not_pushed(monkeypatch):
    with tempfile.TemporaryDirectory() as tmp:
        # DB has REF2, but only REF1 has been adopted -> REF2 is not_tracked.
        state_path, client = _setup(
            monkeypatch, tmp, db_stock={"REF1": 10, "REF2": 3}, adopted={"REF1": (1, 10)},
        )
        report = stock_sync.run_stock_sync(_cfg(state_path))
        assert report["not_tracked"] == 1
        assert report["updated"] == []
        assert client.batch_calls == []


def test_missing_in_db_not_zeroed_by_default(monkeypatch):
    with tempfile.TemporaryDirectory() as tmp:
        # REF1 is tracked but no longer has computed stock in the DB.
        state_path, client = _setup(
            monkeypatch, tmp, db_stock={}, adopted={"REF1": (1, 5)},
        )
        report = stock_sync.run_stock_sync(_cfg(state_path, zero_missing_in_db=False))
        assert report["missing_in_db"] == 0
        assert client.batch_calls == []


def test_missing_in_db_zeroed_when_enabled(monkeypatch):
    with tempfile.TemporaryDirectory() as tmp:
        state_path, client = _setup(
            monkeypatch, tmp, db_stock={}, adopted={"REF1": (1, 5)},
        )
        report = stock_sync.run_stock_sync(_cfg(state_path, zero_missing_in_db=True))
        assert report["missing_in_db"] == 1
        assert report["updated"] == ["REF1"]
        assert client.batch_calls == [[{"id": 1, "manage_stock": True, "stock_quantity": 0}]]


def test_dry_run_does_not_call_batch_update(monkeypatch):
    with tempfile.TemporaryDirectory() as tmp:
        state_path, client = _setup(
            monkeypatch, tmp, db_stock={"REF1": 10}, adopted={"REF1": (1, 5)},
        )
        report = stock_sync.run_stock_sync(_cfg(state_path), dry_run=True)
        assert client.batch_calls == []
        assert len(report["updates_preview"]) == 1
        assert report["updates_preview"][0] == {"sku": "REF1", "stock_quantity": 10}
        # dry run must not persist last_stock
        with StateStore(state_path) as store:
            assert store.get_stock_targets()["REF1"] == (1, 5)


def test_no_targets_does_nothing_gracefully(monkeypatch):
    with tempfile.TemporaryDirectory() as tmp:
        state_path, client = _setup(
            monkeypatch, tmp, db_stock={"REF1": 10}, adopted={},
        )
        report = stock_sync.run_stock_sync(_cfg(state_path))
        assert report["not_tracked"] == 1
        assert report["updated"] == []
        assert client.batch_calls == []
