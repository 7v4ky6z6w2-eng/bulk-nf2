"""Local SQLite state store.

Tracks which Firebird ARTICLE.REF_ART has been synced to which WooCommerce
product, plus a content hash so recurring syncs only push changed articles
and never create duplicates.
"""

import sqlite3


SCHEMA = """
CREATE TABLE IF NOT EXISTS sync_map (
    ref_art TEXT PRIMARY KEY,
    wc_product_id INTEGER,
    content_hash TEXT,
    image_source TEXT,          -- 'blob' | 'none'
    last_synced_at TEXT,
    last_error TEXT
);
"""


class StateStore:
    def __init__(self, path):
        self.path = path
        self.con = sqlite3.connect(path)
        self.con.execute(SCHEMA)
        self.con.commit()

    def close(self):
        self.con.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def get(self, ref_art):
        row = self.con.execute(
            "SELECT ref_art, wc_product_id, content_hash, image_source, "
            "last_synced_at, last_error FROM sync_map WHERE ref_art = ?",
            (ref_art,),
        ).fetchone()
        if not row:
            return None
        keys = ["ref_art", "wc_product_id", "content_hash", "image_source",
                "last_synced_at", "last_error"]
        return dict(zip(keys, row))

    def all_ref_arts(self):
        return {r[0] for r in self.con.execute("SELECT ref_art FROM sync_map")}

    def upsert(self, ref_art, wc_product_id, content_hash, image_source,
               synced_at, error=None):
        self.con.execute(
            "INSERT INTO sync_map (ref_art, wc_product_id, content_hash, "
            "  image_source, last_synced_at, last_error) "
            "VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(ref_art) DO UPDATE SET "
            "  wc_product_id=excluded.wc_product_id, "
            "  content_hash=excluded.content_hash, "
            "  image_source=excluded.image_source, "
            "  last_synced_at=excluded.last_synced_at, "
            "  last_error=excluded.last_error",
            (ref_art, wc_product_id, content_hash, image_source, synced_at, error),
        )
        self.con.commit()

    def record_error(self, ref_art, error, synced_at):
        cur = self.con.execute(
            "UPDATE sync_map SET last_error = ?, last_synced_at = ? WHERE ref_art = ?",
            (error, synced_at, ref_art),
        )
        if cur.rowcount == 0:
            self.con.execute(
                "INSERT INTO sync_map (ref_art, last_error, last_synced_at) "
                "VALUES (?, ?, ?)",
                (ref_art, error, synced_at),
            )
        self.con.commit()

    def delete(self, ref_art):
        self.con.execute("DELETE FROM sync_map WHERE ref_art = ?", (ref_art,))
        self.con.commit()
