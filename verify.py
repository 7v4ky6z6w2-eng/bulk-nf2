"""Outil de diagnostic — à lancer sur chaque poste.

Vérifie :
  1. fbclient.dll présent / fdb importable
  2. Connexion Firebird (lit une désignation pour confirmer WIN1256)
  3. Ping hub
  4. Clé API hub
"""

from __future__ import annotations

import argparse
import os
import sys

_SRC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)


def check(label: str, ok: bool, detail: str = "") -> bool:
    icon = "[OK]" if ok else "[FAIL]"
    msg = "%s %s" % (icon, label)
    if detail:
        msg += " — " + detail
    print(msg)
    return ok


def main() -> None:
    parser = argparse.ArgumentParser(description="Diagnostic PrimeNF Hub")
    parser.add_argument("--store-id", type=int, default=None)
    parser.add_argument("--stores-path", default=None)
    args = parser.parse_args()

    all_ok = True

    # 1. fdb importable
    try:
        import fdb  # type: ignore
        all_ok &= check("fdb importable", True, "version %s" % fdb.__version__)
    except ImportError as exc:
        all_ok &= check("fdb importable", False, str(exc))

    # 2. Chargement stores.json
    try:
        from stores import StoreRegistry, StoresError
        registry = StoreRegistry.load(args.stores_path) if args.stores_path \
            else StoreRegistry.load()
        all_ok &= check("stores.json chargé", True,
                        "%d magasin(s)" % len(registry.stores))
    except Exception as exc:  # noqa: BLE001
        all_ok &= check("stores.json chargé", False, str(exc))
        sys.exit(1)

    # 3. Connexion Firebird locale
    store_id = args.store_id
    if store_id is None:
        # Deviner depuis le premier magasin dont le host est localhost
        for s in registry.stores:
            if s.host in ("localhost", "127.0.0.1", ""):
                store_id = s.id
                break
        if store_id is None:
            store_id = registry.stores[0].id

    store = registry.get(store_id)
    kw = store.connect_kwargs(local=True)
    try:
        import fdb  # type: ignore
        con = fdb.connect(**kw)
        cur = con.cursor()
        cur.execute("SELECT FIRST 1 DESIGNATION FROM ARTICLE")
        row = cur.fetchone()
        desig = (row[0] or "").strip() if row else "(vide)"
        con.close()
        all_ok &= check("Firebird connexion locale (%s)" % store.name, True,
                        "exemple : %s" % desig[:50])
    except Exception as exc:  # noqa: BLE001
        all_ok &= check("Firebird connexion locale", False, str(exc))

    # 3 bis. Lecture du stock (procédure SPSTOCKDEP/SPSTOCK ou table dédiée)
    try:
        from sync.reader import FirebirdReader
        reader = FirebirdReader(kw)
        try:
            stock = reader.read_stock_snapshot()
        finally:
            reader.close()
        all_ok &= check("Stock lisible", bool(stock),
                        "%d article(s)" % len(stock) if stock
                        else "0 ligne — voir les WARNING du log agent")
    except Exception as exc:  # noqa: BLE001
        all_ok &= check("Stock lisible", False, str(exc))

    # 4. Ping hub
    hub_url = registry.hub_url()
    try:
        import requests
        r = requests.get(hub_url + "/api/ping", timeout=5)
        all_ok &= check("Hub joignable (%s)" % hub_url, r.status_code == 200)
    except Exception as exc:  # noqa: BLE001
        all_ok &= check("Hub joignable (%s)" % hub_url, False, str(exc))

    # 5. Clé API
    if registry.hub_api_key:
        try:
            import requests
            r = requests.get(hub_url + "/api/ping",
                             headers={"X-Api-Key": registry.hub_api_key}, timeout=5)
            all_ok &= check("Clé API hub", r.status_code == 200)
        except Exception as exc:  # noqa: BLE001
            all_ok &= check("Clé API hub", False, str(exc))

    print()
    print("=== %s ===" % ("TOUT OK" if all_ok else "DES VÉRIFICATIONS ONT ÉCHOUÉ"))
    sys.exit(0 if all_ok else 1)


if __name__ == "__main__":
    main()
