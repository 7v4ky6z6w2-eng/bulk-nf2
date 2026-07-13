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
    last_error TEXT,
    last_stock INTEGER,         -- last stock_quantity pushed to WooCommerce
    last_regular_price REAL     -- anchor "was" price for auto-sale-on-price-drop
);
"""


class StateStore:
    def __init__(self, path):
        self.path = path
        self.con = sqlite3.connect(path)
        self.con.execute(SCHEMA)
        self._migrate()
        self.con.commit()

    def _migrate(self):
        """Add columns introduced after a store may already exist on disk."""
        existing = {row[1] for row in self.con.execute("PRAGMA table_info(sync_map)")}
        if "last_stock" not in existing:
            self.con.execute("ALTER TABLE sync_map ADD COLUMN last_stock INTEGER")
        if "last_regular_price" not in existing:
            self.con.execute("ALTER TABLE sync_map ADD COLUMN last_regular_price REAL")

    def close(self):
        self.con.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def get(self, ref_art):
        row = self.con.execute(
            "SELECT ref_art, wc_product_id, content_hash, image_source, "
            "last_synced_at, last_error, last_regular_price FROM sync_map WHERE ref_art = ?",
            (ref_art,),
        ).fetchone()
        if not row:
            return None
        keys = ["ref_art", "wc_product_id", "content_hash", "image_source",
                "last_synced_at", "last_error", "last_regular_price"]
        return dict(zip(keys, row))

    def all_ref_arts(self):
        return {r[0] for r in self.con.execute("SELECT ref_art FROM sync_map")}

    def upsert(self, ref_art, wc_product_id, content_hash, image_source,
               synced_at, error=None, last_regular_price=None):
        self.con.execute(
            "INSERT INTO sync_map (ref_art, wc_product_id, content_hash, "
            "  image_source, last_synced_at, last_error, last_regular_price) "
            "VALUES (?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(ref_art) DO UPDATE SET "
            "  wc_product_id=excluded.wc_product_id, "
            "  content_hash=excluded.content_hash, "
            "  image_source=excluded.image_source, "
            "  last_synced_at=excluded.last_synced_at, "
            "  last_error=excluded.last_error, "
            "  last_regular_price=excluded.last_regular_price",
            (ref_art, wc_product_id, content_hash, image_source, synced_at, error, last_regular_price),
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

    # -- adoption / stock tracking -------------------------------------------
    def adopt(self, ref_art, wc_product_id, synced_at):
        """Record REF_ART -> WooCommerce product ID for a product this tool
        did not create itself (e.g. made by an older tool). Sets the
        wc_product_id without clobbering an existing content_hash, so a
        subsequent full sync UPDATES the product (it has an id) instead of
        trying to CREATE a duplicate SKU. A newly-adopted row gets an empty
        content_hash so that first full sync refreshes it once."""
        cur = self.con.execute(
            "UPDATE sync_map SET wc_product_id = ?, last_synced_at = ? WHERE ref_art = ?",
            (wc_product_id, synced_at, ref_art),
        )
        if cur.rowcount == 0:
            self.con.execute(
                "INSERT INTO sync_map (ref_art, wc_product_id, content_hash, "
                "  image_source, last_synced_at) VALUES (?, ?, '', '', ?)",
                (ref_art, wc_product_id, synced_at),
            )
        self.con.commit()

    def get_stock_targets(self):
        """{ref_art: (wc_product_id, last_stock)} for every row that has a
        known WooCommerce product id -- the set stock sync can update
        without having to (re-)enumerate the store over the API."""
        rows = self.con.execute(
            "SELECT ref_art, wc_product_id, last_stock FROM sync_map "
            "WHERE wc_product_id IS NOT NULL"
        )
        return {r[0]: (r[1], r[2]) for r in rows}

    def set_last_stocks(self, items, synced_at):
        """Bulk-record the stock quantities just pushed. 'items' is an
        iterable of (ref_art, qty)."""
        self.con.executemany(
            "UPDATE sync_map SET last_stock = ?, last_synced_at = ? WHERE ref_art = ?",
            [(qty, synced_at, ref) for ref, qty in items],
        )
        self.con.commit()
