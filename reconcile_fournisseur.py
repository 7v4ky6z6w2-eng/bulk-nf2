"""Diagnostic ponctuel (lecture seule) : compare les BL du fournisseur (poste du
père) et les bons de réception déjà présents dans un magasin, pour repérer À LA
MAIN lesquels ont déjà été saisis AVANT que l'outil de synchro automatique ne
tourne.

Pourquoi ce script existe : la synchro automatique (fournisseur_sync.py + hub)
ne peut PAS détecter tout seule qu'une réception déjà saisie manuellement
correspond à un BL du père — une réception saisie à la main ne garde aucune
référence vers le NOPIECE du BL d'origine, donc rien à comparer. Avant
d'activer la synchro sur une période donnée, ce script aide à vérifier
manuellement ce qui a déjà été saisi (par article, sur une période), pour
décider jusqu'où remonter (lookback_days) sans créer de doublons.

Ne modifie RIEN (uniquement des SELECT sur les deux bases).

Usage (depuis le hub, qui doit pouvoir joindre les DEUX Firebird — celui du
magasin cible via stores.json, celui du père via les options --father-*) :

  python reconcile_fournisseur.py --store-id 2 \\
      --father-host 100.x.y.z --father-database "C:\\...\\PRIMEOFFICE2026.FDB" \\
      --father-password ... --days 60 --designation "recharge marqueur"

  python reconcile_fournisseur.py --store-id 2 --father-local \\
      --father-database "C:\\...\\PRIMEOFFICE2026.FDB" --father-password ... \\
      --days 90 --ref "70010"

--father-local : à utiliser si ce script tourne SUR le poste du père lui-même
(connexion Firebird locale) plutôt que depuis le hub via Tailscale/ZeroTier.
"""

from __future__ import annotations

import argparse
import datetime
import os
import sys

_SRC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

import fdb  # type: ignore
from stores import StoreRegistry


def _connect(host, port, database, user, password, charset):
    kwargs = dict(database=database, user=user, password=password, charset=charset)
    if host:
        kwargs["host"] = host
        if port:
            kwargs["port"] = int(port)
    return fdb.connect(**kwargs)


def _article_filter(ref: str | None, designation: str | None):
    """Renvoie (clause_sql, params) — au moins un des deux filtres est requis."""
    clauses, params = [], []
    if ref:
        clauses.append("i.REF_ART = ?")
        params.append(ref)
    if designation:
        clauses.append("a.DESIGNATION CONTAINING ?")
        params.append(designation)
    return " OR ".join(clauses), params


def _fetch_lines(con, code_type_piece: str, cutoff, ref: str | None,
                 designation: str | None, with_tiers: bool) -> list:
    art_where, art_params = _article_filter(ref, designation)
    tiers_join = "LEFT JOIN TIERS t ON t.CODE_TIERS = p.CODE_TIERS" if with_tiers else ""
    tiers_col = "t.RAISON_SOCIALE" if with_tiers else "NULL"
    sql = (
        "SELECT p.NOPIECE, p.DATEPIECE, p.CODE_TIERS, %s, i.REF_ART, "
        "       a.DESIGNATION, i.QTE, i.PRIXHT "
        "FROM PIECE p JOIN ITEM i ON i.NOPIECE = p.NOPIECE "
        "LEFT JOIN ARTICLE a ON a.REF_ART = i.REF_ART %s "
        "WHERE p.CODE_TYPE_PIECE = ? AND p.DATEPIECE >= ? AND (%s) "
        "ORDER BY p.DATEPIECE" % (tiers_col, tiers_join, art_where)
    )
    cur = con.cursor()
    cur.execute(sql, [code_type_piece, cutoff] + art_params)
    rows = cur.fetchall()
    return [{"nopiece": str(r[0]), "date": r[1], "code_tiers": r[2], "raison_sociale": r[3],
            "ref_art": r[4], "designation": r[5], "qte": float(r[6] or 0),
            "prix": float(r[7] or 0)} for r in rows]


def _print_table(title: str, rows: list) -> None:
    print("\n=== %s (%d ligne(s)) ===" % (title, len(rows)))
    if not rows:
        print("  (rien)")
        return
    total_qte = 0.0
    for r in rows:
        total_qte += r["qte"]
        tiers = r.get("raison_sociale") or r.get("code_tiers") or "-"
        print("  %-12s %-14s qte=%-8.2f %-35s @ %-8.2f piece=%-10s tiers=%s" % (
            str(r["date"])[:10], r["ref_art"], r["qte"],
            (r["designation"] or "")[:35], r["prix"], r["nopiece"], tiers))
    print("  ---- total quantité : %.2f (sur %d ligne(s)) ----" % (total_qte, len(rows)))


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--store-id", type=int, required=True,
                    help="Magasin CIBLE (côté réception) — id dans stores.json.")
    ap.add_argument("--stores-path", default=None)
    ap.add_argument("--store-local", action="store_true",
                    help="Connexion locale au magasin cible (si lancé sur son propre poste).")

    ap.add_argument("--father-host", default=None)
    ap.add_argument("--father-port", type=int, default=3050)
    ap.add_argument("--father-database", required=True)
    ap.add_argument("--father-user", default="SYSDBA")
    ap.add_argument("--father-password", default="masterkey")
    ap.add_argument("--father-charset", default="WIN1256")
    ap.add_argument("--father-local", action="store_true",
                    help="Connexion locale (lancé SUR le poste du père).")

    ap.add_argument("--ref", default=None, help="Référence article exacte à comparer.")
    ap.add_argument("--designation", default=None,
                    help="Sous-chaîne de désignation à comparer (insensible à la casse).")
    ap.add_argument("--days", type=int, default=60, help="Nombre de jours à remonter.")
    ap.add_argument("--code-type-piece-bl", default="PC_VE_B",
                    help="Type de pièce des BL côté père (confirmé PC_VE_B).")
    ap.add_argument("--code-type-piece-reception", default="PC_AC_B",
                    help="Type de pièce des bons de réception côté magasin.")
    args = ap.parse_args()

    if not args.ref and not args.designation:
        sys.exit("Indiquez --ref et/ou --designation (au moins un des deux).")

    cutoff = datetime.datetime.now() - datetime.timedelta(days=args.days)

    registry = StoreRegistry.load(args.stores_path) if args.stores_path else StoreRegistry.load()
    store = registry.get(args.store_id)
    store_kw = store.connect_kwargs(local=args.store_local)
    print("Magasin cible : %s (%s:%s)" % (store.name, store_kw.get("host", "localhost"),
                                          store_kw.get("port")))
    father_host = None if args.father_local else args.father_host
    print("Poste du père : %s:%s" % (father_host or "localhost", args.father_port))

    father_con = _connect(father_host, args.father_port, args.father_database,
                          args.father_user, args.father_password, args.father_charset)
    try:
        father_rows = _fetch_lines(father_con, args.code_type_piece_bl, cutoff,
                                   args.ref, args.designation, with_tiers=True)
    finally:
        father_con.close()

    store_con = fdb.connect(**store_kw)
    try:
        store_rows = _fetch_lines(store_con, args.code_type_piece_reception, cutoff,
                                  args.ref, args.designation, with_tiers=False)
    finally:
        store_con.close()

    _print_table("BL du père (%s, %d derniers jours)" % (args.code_type_piece_bl, args.days),
                father_rows)
    _print_table("Réceptions déjà dans %s (%s, %d derniers jours)"
                % (store.name, args.code_type_piece_reception, args.days), store_rows)

    print("\nComparez les deux totaux et les dates ci-dessus à l'œil : si les quantités du "
         "père sur une période correspondent déjà à des réceptions du magasin sur la même "
         "période, c'est probablement déjà saisi à la main — n'activez la synchro automatique "
         "qu'à partir d'une date postérieure (lookback_days / date du premier lancement).")


if __name__ == "__main__":
    main()
