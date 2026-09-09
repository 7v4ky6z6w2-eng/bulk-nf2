#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Nettoyage des articles corrompus (designation contenant des « ? »).
=================================================================

Quand un import a ete fait avec un mauvais charset (ex. WIN1252 au lieu de
WIN1256), l'arabe a ete remplace par des « ? ». Cet outil retrouve ces
articles et les supprime, ainsi que TOUTES leurs lignes liees
(ITEM, TARIF, EQUIV_CBARRES, ARTCOMP), afin d'annuler proprement cet import.
On peut ensuite re-importer le fichier avec le bon charset.

SIMULATION par defaut : rien n'est supprime tant que --apply n'est pas passe.

Usage
-----
    python nettoyer_articles.py --config config.json                 # simulation
    python nettoyer_articles.py --config config.json --apply         # suppression
    python nettoyer_articles.py --config config.json --apply \
        --purge-pieces --purge-familles                              # + bons/familles vides

Options
-------
    --motif TEXTE     sous-chaine cherchee dans la designation (defaut « ? »)
    --apply           supprime reellement (sinon simulation)
    --purge-pieces    supprime aussi les bons de reception devenus vides
    --purge-familles  supprime aussi les familles « ? » devenues sans article
"""

import argparse
import sys

from import_bon_reception import load_config, connect

# Tables (et colonne) referencant ARTICLE.REF_ART, a vider avant l'article.
CHILD_TABLES = [
    ("ITEM", "REF_ART"),
    ("TARIF", "REF_ART"),
    ("EQUIV_CBARRES", "REF_ART"),
    ("ARTCOMP", "CODE_M"),
    ("ARTCOMP", "CODE_COMPOSANT"),
]


def main():
    ap = argparse.ArgumentParser(
        description="Supprime les articles corrompus (designation contenant un motif).")
    ap.add_argument("--config", help="Fichier de configuration JSON")
    ap.add_argument("--db", help="Chemin du .FDB (surcharge la config)")
    ap.add_argument("--motif", default="?",
                    help="Sous-chaine recherchee dans la designation (defaut « ? »)")
    ap.add_argument("--apply", action="store_true",
                    help="Supprime reellement (sinon simulation).")
    ap.add_argument("--purge-pieces", action="store_true",
                    help="Supprime aussi les bons de reception devenus vides.")
    ap.add_argument("--purge-familles", action="store_true",
                    help="Supprime aussi les familles « motif » sans article restant.")
    args = ap.parse_args()

    cfg = load_config(args.config)
    if args.db:
        cfg["database"] = args.db

    con = connect(cfg)
    cur = con.cursor()
    try:
        cur.execute("SELECT REF_ART, DESIGNATION FROM ARTICLE WHERE DESIGNATION CONTAINING ?",
                    (args.motif,))
        bad = cur.fetchall()
        if not bad:
            print("Aucun article corrompu (motif %r) trouve." % args.motif)
            con.rollback()
            return

        refs = [r[0] for r in bad]
        print("Articles corrompus (motif %r) : %d" % (args.motif, len(refs)))
        for ref, des in bad[:30]:
            print("  %-22s %s" % (ref, des))
        if len(bad) > 30:
            print("  ... (+%d autres)" % (len(bad) - 30))

        # Lignes liees + bons concernes
        pieces = set()
        cur.execute("SELECT DISTINCT NOPIECE FROM ITEM WHERE REF_ART = ?", (refs[0],))
        child_counts = {}
        for tbl, col in CHILD_TABLES:
            total = 0
            for ref in refs:
                cur.execute("SELECT COUNT(*) FROM %s WHERE %s = ?" % (tbl, col), (ref,))
                total += cur.fetchone()[0]
            child_counts[(tbl, col)] = total
        for ref in refs:
            cur.execute("SELECT DISTINCT NOPIECE FROM ITEM WHERE REF_ART = ?", (ref,))
            pieces.update(p[0] for p in cur.fetchall() if p[0] is not None)

        print("-" * 50)
        for (tbl, col), n in child_counts.items():
            print("  lignes %s.%s liees : %d" % (tbl, col, n))
        print("  bons de reception concernes : %d" % len(pieces))

        if not args.apply:
            print("\n[SIMULATION] Rien supprime. Relancez avec --apply pour supprimer.")
            con.rollback()
            return

        # Suppression (transaction unique) : enfants -> articles
        for ref in refs:
            for tbl, col in CHILD_TABLES:
                cur.execute("DELETE FROM %s WHERE %s = ?" % (tbl, col), (ref,))
            cur.execute("DELETE FROM ARTICLE WHERE REF_ART = ?", (ref,))

        purged_p = 0
        if args.purge_pieces:
            for nop in pieces:
                cur.execute("SELECT COUNT(*) FROM ITEM WHERE NOPIECE = ?", (nop,))
                if cur.fetchone()[0] == 0:
                    cur.execute("DELETE FROM PIECE WHERE NOPIECE = ?", (nop,))
                    purged_p += 1

        purged_f = 0
        if args.purge_familles:
            cur.execute("SELECT CODEFAMILLE FROM FAMILLE WHERE INTITULE CONTAINING ?",
                        (args.motif,))
            for (code,) in cur.fetchall():
                cur.execute("SELECT COUNT(*) FROM ARTICLE WHERE CODEFAMILLE = ?", (code,))
                if cur.fetchone()[0] > 0:
                    continue
                cur.execute("SELECT COUNT(*) FROM FAMILLE WHERE CODEFAMILLE_M = ?", (code,))
                if cur.fetchone()[0] > 0:
                    continue
                try:
                    cur.execute("DELETE FROM FAMILLE WHERE CODEFAMILLE = ?", (code,))
                    purged_f += 1
                except Exception:
                    pass  # famille encore referencee ailleurs : on la laisse

        con.commit()
        print("\nSupprime : %d article(s) corrompu(s)." % len(refs))
        if args.purge_pieces:
            print("Bons de reception vides supprimes : %d." % purged_p)
        if args.purge_familles:
            print("Familles « %s » sans article supprimees : %d." % (args.motif, purged_f))
        print("Vous pouvez maintenant re-importer le fichier avec le charset WIN1256.")
    except Exception as exc:
        con.rollback()
        print("ECHEC : aucune suppression (transaction annulee).", file=sys.stderr)
        print("Detail : %s" % exc, file=sys.stderr)
        sys.exit(1)
    finally:
        con.close()


if __name__ == "__main__":
    main()
