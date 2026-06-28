#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Reparation de l'encodage (textes importes par erreur en UTF-8).
================================================================

Si un import a ete fait avec le charset UTF8, l'arabe a ete stocke en
octets UTF-8 alors que le logiciel lit en WIN1256 (cp1256) : les noms
apparaissent en « lettres accentuees » illisibles. Cet outil retrouve ces
textes et les ré-écrit dans le bon encodage (cp1256), **sur place** :
les articles, prix, stock et bons de reception restent intacts. Aucun
re-import necessaire.

Repare ARTICLE.DESIGNATION et FAMILLE.INTITULE. Les textes deja corrects
(arabe cp1256, francais) ne sont PAS touches.

SIMULATION par defaut ; --apply pour ecrire.

Usage
-----
    python reparer_encodage.py --config config.json            # simulation
    python reparer_encodage.py --config config.json --apply    # reparation
"""

import argparse
import sys

from import_bon_reception import load_config, connect

# (table, cle primaire, colonne texte) a reparer
TARGETS = [
    ("ARTICLE", "REF_ART", "DESIGNATION"),
    ("FAMILLE", "CODEFAMILLE", "INTITULE"),
]


def is_utf8_text(raw):
    """True si 'raw' (octets bruts) est du texte UTF-8 multi-octets — donc
    stocke par erreur en UTF-8 dans une base cp1256. Le cp1256 (arabe/francais)
    ne forme pratiquement jamais d'UTF-8 valide multi-octets, d'ou la fiabilite."""
    if not any(b >= 0x80 for b in raw):
        return False
    try:
        dec = raw.decode("utf-8")
    except UnicodeDecodeError:
        return False
    return any(ord(c) >= 0x100 for c in dec)   # contient du non-latin (arabe…)


def main():
    ap = argparse.ArgumentParser(
        description="Repare les textes stockes par erreur en UTF-8 vers cp1256 (WIN1256).")
    ap.add_argument("--config", help="Fichier de configuration JSON")
    ap.add_argument("--db", help="Chemin du .FDB (surcharge la config)")
    ap.add_argument("--apply", action="store_true",
                    help="Ecrit reellement les corrections (sinon simulation).")
    args = ap.parse_args()

    cfg = dict(load_config(args.config))
    if args.db:
        cfg["database"] = args.db
    # Lecture/ecriture en octets bruts 1:1 (ISO8859_1) : on maitrise les octets.
    cfg["charset"] = "ISO8859_1"

    con = connect(cfg)
    cur = con.cursor()
    try:
        total = 0
        fixed = 0
        for table, key, col in TARGETS:
            cur.execute("SELECT %s, %s FROM %s WHERE %s IS NOT NULL" % (key, col, table, col))
            rows = cur.fetchall()
            updates = []
            for k, val in rows:
                raw = val.encode("latin-1")
                if is_utf8_text(raw):
                    correct = raw.decode("utf-8")                  # vrai texte (arabe)
                    newbytes = correct.encode("cp1256", "replace")  # encodage du logiciel
                    updates.append((k, newbytes.decode("latin-1"), correct))
            if updates:
                print("== %s.%s : %d texte(s) a reparer ==" % (table, col, len(updates)))
                for k, _, correct in updates[:20]:
                    print("  %-22s %s" % (k, correct))
                if len(updates) > 20:
                    print("  ... (+%d autres)" % (len(updates) - 20))
            total += len(updates)
            if args.apply:
                for k, newstr, _ in updates:
                    cur.execute("UPDATE %s SET %s = ? WHERE %s = ?" % (table, col, key),
                                (newstr, k))
                    fixed += 1

        print("A_TRAITER: %d" % total)
        if total == 0:
            print("Aucun texte UTF-8 mal encode trouve (rien a reparer).")
            con.rollback()
            return
        if not args.apply:
            print("\n[SIMULATION] Rien modifie. Relancez avec --apply pour reparer.")
            con.rollback()
            return
        con.commit()
        print("\nRepare : %d texte(s) ré-encodé(s) en WIN1256." % fixed)
        print("Les noms arabes s'affichent maintenant correctement dans le logiciel.")
    except Exception as exc:  # noqa: BLE001
        con.rollback()
        print("ECHEC : aucune modification (transaction annulee).", file=sys.stderr)
        print("Detail : %s" % exc, file=sys.stderr)
        sys.exit(1)
    finally:
        con.close()


if __name__ == "__main__":
    main()
