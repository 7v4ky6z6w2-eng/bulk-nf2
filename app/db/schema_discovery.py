#!/usr/bin/env python3
"""One-off diagnostic: run this against the REAL .fdb before trusting any
field mapping in sync/engine.py.

It confirms, straight from Firebird's system catalog (not a guess from a
strings dump):
  - the exact columns/types of ARTICLE, EQUIV_CBARRES, FAMILLE, STOCK,
    FICHE_STOCK (whichever of the stock tables actually exist),
  - whether FAMILLE.BOUTIQ_VISIBLE (or an ARTICLE-level equivalent) is
    actually populated in this install,
  - what ARTICLE.PHOTO actually contains on a sample of rows (a real
    embedded image vs. NULL/placeholder bytes), using Pillow to validate.

Usage:
    python -m app.db.schema_discovery --config config.json
"""

import argparse
import sys

from app.config import load_config
from app.db.firebird_client import connect

CANDIDATE_TABLES = ["ARTICLE", "EQUIV_CBARRES", "FAMILLE", "STOCK", "FICHE_STOCK"]


def list_columns(cur, table):
    cur.execute(
        "SELECT TRIM(rf.RDB$FIELD_NAME), f.RDB$FIELD_TYPE, f.RDB$FIELD_LENGTH, "
        "       f.RDB$FIELD_SUB_TYPE, rf.RDB$NULL_FLAG "
        "FROM RDB$RELATION_FIELDS rf "
        "JOIN RDB$FIELDS f ON f.RDB$FIELD_NAME = rf.RDB$FIELD_SOURCE "
        "WHERE rf.RDB$RELATION_NAME = ? "
        "ORDER BY rf.RDB$FIELD_POSITION",
        (table,),
    )
    return cur.fetchall()


def table_exists(cur, table):
    cur.execute("SELECT 1 FROM RDB$RELATIONS WHERE RDB$RELATION_NAME = ?", (table,))
    return cur.fetchone() is not None


def sample_photo_blobs(cur, sample_size=10):
    try:
        from PIL import Image
    except ImportError:
        print("  (Pillow not installed -- skipping PHOTO content check)")
        return
    import io

    cur.execute(f"SELECT FIRST {sample_size} REF_ART, PHOTO FROM ARTICLE "
                "WHERE PHOTO IS NOT NULL")
    rows = cur.fetchall()
    if not rows:
        print("  No ARTICLE rows have a non-NULL PHOTO in the first sample.")
        return
    real_images = 0
    for ref, blob in rows:
        data = blob.read() if hasattr(blob, "read") else blob
        if not data:
            continue
        try:
            img = Image.open(io.BytesIO(data))
            img.verify()
            real_images += 1
            print(f"  REF_ART={ref}: real image ({img.format}, {len(data)} bytes)")
        except Exception:
            print(f"  REF_ART={ref}: PHOTO present but NOT a decodable image "
                  f"({len(data)} bytes)")
    print(f"  -> {real_images}/{len(rows)} sampled non-NULL PHOTO blobs are real images")


def sample_boutiq_visible(cur):
    cur.execute("SELECT CODEFAMILLE, BOUTIQ_VISIBLE FROM FAMILLE")
    rows = cur.fetchall()
    if not rows:
        print("  FAMILLE has no rows.")
        return
    populated = [r for r in rows if r[1] is not None]
    visible = [r for r in rows if r[1]]
    print(f"  FAMILLE rows: {len(rows)}, BOUTIQ_VISIBLE populated: {len(populated)}, "
          f"flagged visible: {len(visible)}")


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--config", required=True, help="Path to config.json")
    args = ap.parse_args()

    cfg = load_config(args.config)
    con = connect(cfg)
    try:
        cur = con.cursor()
        for table in CANDIDATE_TABLES:
            if not table_exists(cur, table):
                print(f"[{table}] -- does not exist in this database")
                continue
            print(f"[{table}]")
            for name, ftype, flen, subtype, null_flag in list_columns(cur, table):
                nullable = "NOT NULL" if null_flag else "nullable"
                print(f"  {name:<25} type={ftype} subtype={subtype} "
                      f"len={flen} {nullable}")

        if table_exists(cur, "ARTICLE"):
            print("\n[ARTICLE.PHOTO content check]")
            sample_photo_blobs(cur)

        if table_exists(cur, "FAMILLE"):
            print("\n[FAMILLE.BOUTIQ_VISIBLE check]")
            sample_boutiq_visible(cur)
    finally:
        con.close()


if __name__ == "__main__":
    sys.exit(main())
