import copy
import os
import tempfile

from app.config import DEFAULT_CONFIG
from app.sync import name_resync
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
    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.batch_calls = []

    def batch_products(self, create=None, update=None, delete=None, chunk_size=100, progress_fn=None):
        self.batch_calls.append(update or [])
        return {"create": [], "update": [{"id": u["id"]} for u in (update or [])], "delete": []}


def _article(ref_art, designation):
    return {"ref_art": ref_art, "designation": designation}


def _setup(monkeypatch, tmp, articles, adopted):
    """adopted: dict {ref_art: wc_product_id} seeded into the store."""
    state_path = os.path.join(tmp, "state.sqlite3")
    with StateStore(state_path) as store:
        for ref_art, wc_id in adopted.items():
            store.adopt(ref_art, wc_id, "2026-01-01T00:00:00")

    client = FakeWooCommerceClient()
    monkeypatch.setattr(name_resync, "connect_firebird", lambda cfg: DummyConnection())
    monkeypatch.setattr(name_resync.queries, "fetch_familles", lambda con: {})
    monkeypatch.setattr(name_resync.queries, "fetch_articles",
                         lambda con, familles, filter_boutique_visible: articles)
    monkeypatch.setattr(name_resync, "WooCommerceClient", lambda **kwargs: client)
    return state_path, client


def test_resyncs_name_for_tracked_articles(monkeypatch):
    with tempfile.TemporaryDirectory() as tmp:
        state_path, client = _setup(
            monkeypatch, tmp,
            articles=[_article("REF1", "STYL BIL BLU réf.42")],
            adopted={"REF1": 5},
        )
        report = name_resync.run_name_resync(_cfg(state_path))
        assert report["updated"] == ["REF1"]
        assert client.batch_calls == [[{"id": 5, "name": "STYL BIL BLU réf.42"}]]


def test_untracked_article_is_skipped(monkeypatch):
    with tempfile.TemporaryDirectory() as tmp:
        state_path, client = _setup(
            monkeypatch, tmp,
            articles=[_article("REF1", "Name 1"), _article("REF2", "Name 2")],
            adopted={"REF1": 5},  # REF2 never synced -- nothing to fix
        )
        report = name_resync.run_name_resync(_cfg(state_path))
        assert report["updated"] == ["REF1"]
        assert report["not_tracked"] == 1
        assert client.batch_calls == [[{"id": 5, "name": "Name 1"}]]


def test_dry_run_does_not_call_batch_update(monkeypatch):
    with tempfile.TemporaryDirectory() as tmp:
        state_path, client = _setup(
            monkeypatch, tmp,
            articles=[_article("REF1", "Name 1")],
            adopted={"REF1": 5},
        )
        report = name_resync.run_name_resync(_cfg(state_path), dry_run=True)
        assert client.batch_calls == []
        assert report["payloads"] == [{"ref_art": "REF1", "id": 5, "name": "Name 1"}]


def test_no_targets_does_nothing_gracefully(monkeypatch):
    with tempfile.TemporaryDirectory() as tmp:
        state_path, client = _setup(
            monkeypatch, tmp,
            articles=[_article("REF1", "Name 1")],
            adopted={},
        )
        report = name_resync.run_name_resync(_cfg(state_path))
        assert report["not_tracked"] == 1
        assert report["updated"] == []
        assert client.batch_calls == []


def test_unconfirmed_update_is_reported_as_error(monkeypatch):
    with tempfile.TemporaryDirectory() as tmp:
        state_path, _client = _setup(
            monkeypatch, tmp,
            articles=[_article("REF1", "Name 1")],
            adopted={"REF1": 5},
        )

        class FlakyClient:
            def __init__(self, **kwargs):
                pass

            def batch_products(self, create=None, update=None, delete=None, chunk_size=100, progress_fn=None):
                return {"create": [], "update": [{"id": 5, "error": "boom"}], "delete": []}

        monkeypatch.setattr(name_resync, "WooCommerceClient", FlakyClient)
        report = name_resync.run_name_resync(_cfg(state_path))
        assert report["updated"] == []
        assert report["errors"] == [{"ref_art": "REF1", "error": "update not confirmed"}]
