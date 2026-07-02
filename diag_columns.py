"""Diagnostic ponctuel : liste les colonnes d'une table Firebird + 2 lignes
d'exemple, pour identifier les noms réels de colonnes (ex. caisse) qui varient
d'une installation Netfact2/PrimeOffice à l'autre.

Usage :
  python diag_columns.py --store-id 1 --table PIECE
  python diag_columns.py --store-id 1 --table PIECE --like CAISSE
"""

from __future__ import annotations

import argparse
import os
import sys

_SRC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

import fdb  # type: ignore
from stores import StoreRegistry


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--store-id", type=int, required=True)
    ap.add_argument("--table", required=True, help="Nom de la table Firebird (ex. PIECE)")
    ap.add_argument("--like", default=None,
                    help="N'afficher que les colonnes contenant ce texte (ex. CAISSE)")
    ap.add_argument("--stores-path", default=None)
    ap.add_argument("--sample", type=int, default=2, help="Nombre de lignes d'exemple")
    args = ap.parse_args()

    registry = StoreRegistry.load(args.stores_path) if args.stores_path else StoreRegistry.load()
    store = registry.get(args.store_id)
    kw = store.connect_kwargs(local=True)
    con = fdb.connect(**kw)
    cur = con.cursor()

    table = args.table.upper()
    cur.execute(
        "SELECT TRIM(rf.RDB$FIELD_NAME) FROM RDB$RELATION_FIELDS rf "
        "WHERE rf.RDB$RELATION_NAME = ? ORDER BY rf.RDB$FIELD_POSITION", (table,))
    cols = [r[0] for r in cur.fetchall()]

    if not cols:
        print("Table '%s' introuvable (ou aucune colonne)." % table)
        return

    print("=== Colonnes de %s (%d) ===" % (table, len(cols)))
    for c in cols:
        if args.like and args.like.upper() not in c.upper():
            continue
        print("  ", c)

    if args.sample > 0:
        print("\n=== %d ligne(s) d'exemple ===" % args.sample)
        col_list = ", ".join(cols)
        cur.execute("SELECT FIRST %d %s FROM %s" % (args.sample, col_list, table))
        rows = cur.fetchall()
        for row in rows:
            print("-" * 40)
            for c, v in zip(cols, row):
                print("  %-25s = %r" % (c, v))

    con.close()


if __name__ == "__main__":
    main()
