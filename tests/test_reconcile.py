import copy
import os
import tempfile

from app.config import DEFAULT_CONFIG
from app.sync import reconcile
from app.sync.state_store import StateStore


def _cfg(state_path):
    cfg = copy.deepcopy(DEFAULT_CONFIG)
    cfg["state_db_path"] = state_path
    cfg["woocommerce"]["site_url"] = "https://example.com"
    cfg["woocommerce"]["consumer_key"] = "ck"
    cfg["woocommerce"]["consumer_secret"] = "cs"
    return cfg


class DummyConnection:
    def close(self):
        pass


class FakeWooCommerceClient:
    def __init__(self, products, **kwargs):
        self._products = products

    def fetch_all_products(self, fields=None):
        return self._products


def _patch(monkeypatch, ref_arts, products):
    monkeypatch.setattr(reconcile, "connect_firebird", lambda cfg: DummyConnection())
    monkeypatch.setattr(reconcile.queries, "fetch_all_ref_arts", lambda con: ref_arts)
    monkeypatch.setattr(reconcile, "WooCommerceClient",
                        lambda **kwargs: FakeWooCommerceClient(products))


def test_adopt_matches_products_by_sku(monkeypatch):
    with tempfile.TemporaryDirectory() as tmp:
        state_path = os.path.join(tmp, "state.sqlite3")
        _patch(
            monkeypatch,
            ref_arts={"REF1", "REF2", "REF3"},
            products=[
                {"id": 101, "sku": "REF1"},
                {"id": 102, "sku": "REF2"},
                {"id": 103, "sku": "NOT_AN_ARTICLE"},
                {"id": 104, "sku": ""},  # no sku -> ignored
            ],
        )
        report = reconcile.run_adopt(_cfg(state_path))

        assert report["adopted"] == 2
        assert report["unmatched_wc"] == 1  # NOT_AN_ARTICLE (empty sku not counted)
        assert report["db_total"] == 3

        with StateStore(state_path) as store:
            targets = store.get_stock_targets()
            assert targets["REF1"][0] == 101
            assert targets["REF2"][0] == 102
            assert "NOT_AN_ARTICLE" not in targets


def test_adopt_sets_wc_id_so_next_sync_updates_not_creates(monkeypatch):
    with tempfile.TemporaryDirectory() as tmp:
        state_path = os.path.join(tmp, "state.sqlite3")
        _patch(monkeypatch, ref_arts={"REF1"}, products=[{"id": 500, "sku": "REF1"}])
        reconcile.run_adopt(_cfg(state_path))

        with StateStore(state_path) as store:
            row = store.get("REF1")
            assert row["wc_product_id"] == 500
            # empty content_hash -> a later full sync will UPDATE (has id), not
            # try to CREATE a duplicate SKU.
            assert row["content_hash"] == ""


def test_adopt_does_not_clobber_existing_content_hash(monkeypatch):
    with tempfile.TemporaryDirectory() as tmp:
        state_path = os.path.join(tmp, "state.sqlite3")
        with StateStore(state_path) as store:
            store.upsert("REF1", 500, "existinghash", "none", "2026-01-01T00:00:00")

        _patch(monkeypatch, ref_arts={"REF1"}, products=[{"id": 500, "sku": "REF1"}])
        reconcile.run_adopt(_cfg(state_path))

        with StateStore(state_path) as store:
            assert store.get("REF1")["content_hash"] == "existinghash"
