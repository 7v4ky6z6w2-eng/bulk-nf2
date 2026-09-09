#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Diagnostic d'encodage : lit les octets BRUTS de désignations en base et
indique comment elles sont stockées (cp1256 / cp1252 / UTF-8 / latin).

But : comparer un article qui s'affiche CORRECTEMENT dans votre logiciel
(ex. saisi à la main) avec ceux importés, pour déterminer l'encodage que
votre logiciel attend réellement.

Usage
-----
    # inspecter des articles précis (références séparées par des virgules) :
    python diagnostic_encodage.py --config config.json --ref "REF1,REF2"

    # sinon, balayer les premières désignations non-ASCII trouvées :
    python diagnostic_encodage.py --config config.json
"""

import argparse
import sys

from import_bon_reception import load_config, connect


def classify(raw):
    """Devine l'encodage des octets bruts d'un texte."""
    if not any(b >= 0x80 for b in raw):
        return "ASCII (pas d'accent / pas d'arabe)"
    # UTF-8 ?
    try:
        dec = raw.decode("utf-8")
        if any(ord(c) >= 0x100 for c in dec):
            return "UTF-8 (ex: importé en UTF8) → %r" % dec
    except UnicodeDecodeError:
        pass
    # cp1256 arabe ?
    cp1256 = raw.decode("cp1256", "replace")
    if any("؀" <= c <= "ۿ" for c in cp1256):
        return "cp1256 / WIN1256 (arabe) → %r" % cp1256
    # sinon latin
    return "Latin/Western (cp1252) → %r" % raw.decode("cp1252", "replace")


def main():
    ap = argparse.ArgumentParser(description="Diagnostic d'encodage des désignations.")
    ap.add_argument("--config", help="Fichier de configuration JSON")
    ap.add_argument("--db", help="Chemin du .FDB (surcharge la config)")
    ap.add_argument("--ref", help="Références précises à inspecter (séparées par des virgules)")
    ap.add_argument("--limit", type=int, default=15, help="Nb de lignes en mode balayage")
    args = ap.parse_args()

    cfg = dict(load_config(args.config))
    if args.db:
        cfg["database"] = args.db
    cfg["charset"] = "ISO8859_1"   # lecture des octets bruts 1:1

    con = connect(cfg)
    cur = con.cursor()
    try:
        if args.ref:
            refs = [r.strip() for r in args.ref.split(",") if r.strip()]
            for ref in refs:
                cur.execute("SELECT REF_ART, DESIGNATION FROM ARTICLE WHERE REF_ART = ?", (ref,))
                row = cur.fetchone()
                if not row:
                    print("%-22s : (introuvable)" % ref); continue
                raw = (row[1] or "").encode("latin-1")
                print("REF %s" % row[0])
                print("  octets : %s" % raw[:48].hex())
                print("  verdict: %s" % classify(raw))
        else:
            cur.execute("SELECT REF_ART, DESIGNATION FROM ARTICLE WHERE DESIGNATION IS NOT NULL")
            shown = 0
            for ref, des in cur:
                raw = des.encode("latin-1")
                if not any(b >= 0x80 for b in raw):
                    continue
                print("REF %-20s octets=%s" % (ref, raw[:36].hex()))
                print("    %s" % classify(raw))
                shown += 1
                if shown >= args.limit:
                    break
            if shown == 0:
                print("Aucune désignation avec caractères spéciaux trouvée.")
    finally:
        con.close()


if __name__ == "__main__":
    main()
