import os
import sqlite3
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


def test_adopt_inserts_and_updates_without_clobbering_hash():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "state.sqlite3")
        with StateStore(path) as store:
            # new row
            store.adopt("A1", 10, "t")
            assert store.get("A1")["wc_product_id"] == 10
            assert store.get("A1")["content_hash"] == ""

            # existing row keeps its content_hash
            store.upsert("A2", 20, "keepme", "none", "t")
            store.adopt("A2", 99, "t2")
            row = store.get("A2")
            assert row["wc_product_id"] == 99
            assert row["content_hash"] == "keepme"


def test_stock_targets_and_set_last_stocks():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "state.sqlite3")
        with StateStore(path) as store:
            store.adopt("A1", 10, "t")
            store.adopt("A2", 20, "t")
            # a row without a wc_product_id must not appear as a target
            store.record_error("A3", "no id yet", "t")

            targets = store.get_stock_targets()
            assert set(targets) == {"A1", "A2"}
            assert targets["A1"] == (10, None)  # last_stock unset initially

            store.set_last_stocks([("A1", 7), ("A2", 3)], "t2")
            targets = store.get_stock_targets()
            assert targets["A1"] == (10, 7)
            assert targets["A2"] == (20, 3)


def test_last_stock_column_added_to_preexisting_db():
    # A store created before last_stock existed must gain the column on open.
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "state.sqlite3")
        con = sqlite3.connect(path)
        con.execute(
            "CREATE TABLE sync_map (ref_art TEXT PRIMARY KEY, wc_product_id INTEGER, "
            "content_hash TEXT, image_source TEXT, last_synced_at TEXT, last_error TEXT)"
        )
        con.execute("INSERT INTO sync_map (ref_art, wc_product_id) VALUES ('A1', 5)")
        con.commit()
        con.close()

        with StateStore(path) as store:
            store.set_last_stocks([("A1", 9)], "t")
            assert store.get_stock_targets()["A1"] == (5, 9)
