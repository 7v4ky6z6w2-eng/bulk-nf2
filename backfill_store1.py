#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Backfill initial : lit la base Firebird du magasin HUB (magasin 1) et la
charge directement dans central.db, sans passer par le réseau ni l'API HTTP.

À lancer une fois sur le poste hub, après avoir créé central.db. Ensuite le
magasin 1 est resynchronisé par l'agent comme les autres (en local).

    python backfill_store1.py --stores stores.json --db central.db
"""

from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "src"))

from stores import StoreRegistry            # noqa: E402
from hub import central_db as cdb           # noqa: E402
from sync.reader import FirebirdReader      # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description="Backfill du magasin hub vers central.db")
    ap.add_argument("--stores", default="stores.json")
    ap.add_argument("--db", default="central.db")
    args = ap.parse_args()

    reg = StoreRegistry.load(args.stores)
    hub = reg.hub()
    print("Backfill du magasin %s (%s)…" % (hub.id, hub.name))

    cdb.init_db(args.db)
    con = cdb.connect(args.db)
    reader = FirebirdReader(hub.connect_kwargs(local=True))
    try:
        sid = cdb.start_sync(con, hub.id, hub.name)
        total = 0
        for table, rows in reader.read_all(full=True):
            n = cdb.upsert_batch(con, table, hub.id, rows)
            cdb.add_rows_pushed(con, sid, n)
            total += n
            con.commit()
            print("  %-22s %6d lignes" % (table, n))
        cdb.finish_sync(con, sid, total, "ok")
        print("Terminé : %d lignes chargées dans %s" % (total, args.db))
    except Exception as exc:  # noqa: BLE001
        print("ÉCHEC backfill : %s" % exc, file=sys.stderr)
        return 1
    finally:
        reader.close()
        con.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
