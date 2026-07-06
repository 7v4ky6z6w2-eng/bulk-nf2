import os
import tempfile

from app.sync.state_store import StateStore


def test_upsert_and_get_roundtrip():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "state.sqlite3")
        with StateStore(path) as store:
            assert store.get("ABC123") is None
            store.upsert("ABC123", 42, "hash1", "blob", "2026-01-01T00:00:00")
            row = store.get("ABC123")
            assert row["wc_product_id"] == 42
            assert row["content_hash"] == "hash1"
            assert row["image_source"] == "blob"

            store.upsert("ABC123", 42, "hash2", "none", "2026-01-02T00:00:00")
            row = store.get("ABC123")
            assert row["content_hash"] == "hash2"
            assert row["image_source"] == "none"


def test_all_ref_arts_and_delete():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "state.sqlite3")
        with StateStore(path) as store:
            store.upsert("A1", 1, "h", "none", "t")
            store.upsert("A2", 2, "h", "none", "t")
            assert store.all_ref_arts() == {"A1", "A2"}
            store.delete("A1")
            assert store.all_ref_arts() == {"A2"}


def test_record_error_creates_row_if_missing():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "state.sqlite3")
        with StateStore(path) as store:
            store.record_error("NEW1", "boom", "t")
            row = store.get("NEW1")
            assert row["last_error"] == "boom"
