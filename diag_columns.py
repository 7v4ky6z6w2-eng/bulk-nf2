"""Diagnostic ponctuel : liste les colonnes d'une table Firebird + 2 lignes
d'exemple, pour identifier les noms réels de colonnes (ex. caisse) qui varient
d'une installation Netfact2/PrimeOffice à l'autre.

Se connecte par défaut EN DISTANT (via l'adresse Tailscale du magasin dans
stores.json, port Firebird 3050) — utile pour lancer ce diagnostic depuis le
hub (le seul poste avec Python) contre la base d'un magasin 2/3 qui n'a pas
Python. Ajoutez --local si vous lancez ce script SUR le poste du magasin
lui-même (connexion Firebird locale).

Usage :
  python diag_columns.py --store-id 2 --table PIECE            (depuis le hub, vers le magasin 2)
  python diag_columns.py --store-id 1 --table PIECE --local    (sur le poste du magasin 1 lui-même)
  python diag_columns.py --store-id 2 --table PIECE --like CAISSE
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
    ap.add_argument("--local", action="store_true",
                    help="Connexion locale (localhost) : à utiliser si ce script "
                         "tourne SUR le poste du magasin lui-même, pas depuis le hub.")
    ap.add_argument("--order-by", default=None,
                    help="Colonne de tri (ex. DATEPIECE) — sans ça, les lignes "
                         "retournées sont arbitraires (souvent de vieilles données).")
    ap.add_argument("--desc", action="store_true", help="Tri décroissant (plus récent d'abord)")
    ap.add_argument("--where", default=None,
                    help="Condition SQL brute (ex. \"CODE_MODE_REGL IS NOT NULL\")")
    args = ap.parse_args()

    registry = StoreRegistry.load(args.stores_path) if args.stores_path else StoreRegistry.load()
    store = registry.get(args.store_id)
    kw = store.connect_kwargs(local=args.local)
    print("Connexion à %s:%s (%s)..." % (kw["host"], kw["port"], store.name))
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
        sql = "SELECT FIRST %d %s FROM %s" % (args.sample, col_list, table)
        if args.where:
            sql += " WHERE %s" % args.where
        if args.order_by:
            sql += " ORDER BY %s%s" % (args.order_by, " DESC" if args.desc else "")
        print("(%s)" % sql)
        cur.execute(sql)
        rows = cur.fetchall()
        for row in rows:
            print("-" * 40)
            for c, v in zip(cols, row):
                print("  %-25s = %r" % (c, v))

    con.close()


if __name__ == "__main__":
    main()
