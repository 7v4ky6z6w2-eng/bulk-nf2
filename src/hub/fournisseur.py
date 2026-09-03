"""Synchro fournisseur : convertit chaque ligne de bon de livraison, chez un
fournisseur qui utilise le même logiciel (Netfact2/PRIME) sur son propre
poste, en ligne de bon de réception dans le magasin destinataire correspondant.

L'exe qui tourne CHEZ le fournisseur ne fait que lire son Firebird local et
POSTer les lignes brutes ici (voir /api/fournisseur/lines) — tout le
rapprochement d'articles, la décision auto/attente, et l'écriture réelle
(via submit_op, comme le reste du hub) se passent ICI, pas dans l'exe :
plus facile à corriger sans redéployer un binaire sur un poste distant.

Règles (décidées avec l'utilisateur, ne pas les redériver) :
  * Rapprochement : référence/code-barres d'abord (status 'exact'), nom en
    repli (status 'matched'). Un match par NOM SEUL n'est jamais automatique
    (comme la correspondance manuelle du reste de l'appli) -> file d'attente
    fournisseur_pending, à valider sur le tableau de bord web.
  * Aucune correspondance -> nouvel article, prix de vente TOUJOURS en repli
    automatique (marge), jamais copié du fournisseur — modifiable ensuite.
  * Une ligne déjà synchronisée dont qté/prix change ENSUITE -> édite la
    réception déjà créée EN PLACE (opération item_edit), ne crée jamais une
    seconde réception pour la même ligne d'origine.
  * code_tiers -> magasin : table éditable (fournisseur_tiers_map), jamais
    codée en dur — une ligne dont le code_tiers n'est pas mappé est ignorée
    (ce n'est pas une erreur : le fournisseur a sûrement d'autres clients).
  * TVA toujours 0 : le fournisseur est la MÊME entreprise que ses magasins
    (transfert interne), pas un achat externe soumis à TVA. fournisseur_sync.py
    n'envoie donc jamais la TVA lue dans Firebird — toujours 0.
  * Le NOPIECE du BL d'origine est stocké dans PIECE.REFDOC de la réception
    créée (côté magasin) : sert de repli pour retrouver une réception créée
    HORS LIGNE puis appliquée par l'agent sans jamais repasser par ici (voir
    fournisseur_dest_nopiece_by_refdoc) — sinon fournisseur_sync_state ne
    l'apprend jamais, bloquant toute édition ultérieure et risquant une
    réception en double si le fournisseur ajoute un article au même BL.
"""

from __future__ import annotations

import os
import sqlite3
import sys

_VENDOR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "vendor")
_BDR_DIR = os.path.join(_VENDOR, "bdr")
if _BDR_DIR not in sys.path:
    sys.path.insert(0, _BDR_DIR)

from hub.central_db import (
    fournisseur_mapping, fournisseur_sync_state_get, fournisseur_sync_state_set,
    fournisseur_pending_add, fournisseur_known_dest_nopiece,
    fournisseur_dest_nopiece_by_refdoc, fournisseur_dest_noitem_by_ref,
)
from hub.ops import submit_op


def _match_line(con: sqlite3.Connection, store_id: int, line: dict) -> dict:
    """Rapproche une ligne fournisseur avec le catalogue MIROIR (central.db)
    du magasin destinataire, via le même moteur que l'import BDR classique
    (best_match_for_line) — juste borné à un magasin via le paramètre
    extra_where de build_article_index (ajouté pour cet usage)."""
    import import_bon_reception as bdr  # type: ignore
    cur = con.cursor()
    by_number, exact_refs, prix, common_words = bdr.build_article_index(
        cur, "store_id=?", (store_id,))
    fake_line = {"ref_art": line.get("ref_art") or "",
                "designation": line.get("designation")}
    return bdr.best_match_for_line(fake_line, by_number, exact_refs, prix,
                                   0.60, common_words)


def _build_bdr_line(ref_art: str, line: dict) -> dict:
    # Le fournisseur est le même commerçant que ses magasins (transfert
    # interne, pas un achat externe) : TVA toujours 0 en pratique — donc PAS
    # "line.get('tva') or 0" (0 est falsy, ça écraserait un 0 valide envoyé
    # volontairement). On distingue explicitement l'ABSENCE de la clé (vieille
    # ligne en attente créée avant l'ajout de la colonne tva, NULL en base)
    # d'une valeur 0 légitime.
    tva = line.get("tva")
    return {
        "ref_art": ref_art,
        "designation": line.get("designation") or ref_art,
        "qte": float(line.get("qte") or 0),
        "prix": float(line.get("prix") or 0),
        "tva": tva if tva is not None else 0,
        "famille": "",
        "code_barres": (line.get("code_barres") or "").strip(),
    }


def process_line(con: sqlite3.Connection, registry, line: dict) -> dict:
    """Traite UNE ligne brute venant de l'exe fournisseur.

    `line` : {src_nopiece, src_noitem, code_tiers, ref_art, designation, qte,
    prix, tva, code_barres}. Renvoie {"status": "applied"|"queued"|
    "unchanged"|"pending"|"skipped"|"error", ...}."""
    mapping = fournisseur_mapping(con)
    m = mapping.get(str(line.get("code_tiers") or ""))
    if not m:
        return {"status": "skipped", "reason": "code_tiers non mappé à un magasin"}
    store_id = m["store_id"]
    src_nopiece = str(line.get("src_nopiece") or "")
    src_noitem = str(line.get("src_noitem") or "")
    if not src_nopiece or not src_noitem:
        return {"status": "error", "error": "src_nopiece/src_noitem manquant"}
    qte = float(line.get("qte") or 0)
    prix = float(line.get("prix") or 0)

    state = fournisseur_sync_state_get(con, store_id, src_nopiece, src_noitem)
    if state:
        if state["last_qte"] == qte and state["last_prix"] == prix:
            return {"status": "unchanged", "store_id": store_id}
        dest_nopiece = state["dest_nopiece"]
        dest_noitem = state["dest_noitem"]
        if not dest_nopiece or not dest_noitem:
            # La création a pu passer par la file (magasin hors ligne au
            # moment du dernier passage) puis être appliquée par l'agent SANS
            # que fournisseur_sync_state en soit jamais informé (rien ne
            # revient le dire) — on cherche la réception via REFDOC avant
            # d'abandonner (voir docstring du module) : se corrige tout seul
            # dès que l'agent a synchronisé le miroir, pas besoin d'attendre
            # un changement supplémentaire de cette ligne.
            dest_nopiece = dest_nopiece or fournisseur_dest_nopiece_by_refdoc(
                con, store_id, src_nopiece)
            if not dest_nopiece:
                return {"status": "pending_creation", "store_id": store_id}
            dest_noitem = dest_noitem or fournisseur_dest_noitem_by_ref(
                con, store_id, dest_nopiece, state["dest_ref_art"])
            if not dest_noitem:
                return {"status": "pending_creation", "store_id": store_id}
        edits = [{"nopiece": dest_nopiece, "noitem": dest_noitem,
                 "qte": qte, "prix": prix, "maj_prix_achat": True}]
        result = submit_op(con, registry, store_id, "item_edit", {"edits": edits})
        if result.get("status") in ("applied", "queued"):
            fournisseur_sync_state_set(con, store_id, src_nopiece, src_noitem,
                                       state["dest_ref_art"], dest_nopiece,
                                       dest_noitem, qte, prix)
        result["store_id"] = store_id
        return result

    match = _match_line(con, store_id, line)
    if match["status"] == "matched":
        # Rapprochement par NOM seulement (pas de référence/code-barres
        # identique) : jamais automatique, comme la correspondance manuelle
        # ailleurs dans l'appli — un humain doit confirmer sur le dashboard.
        fournisseur_pending_add(con, store_id, src_nopiece, src_noitem,
                                line.get("ref_art"), line.get("designation"),
                                qte, prix, line.get("code_barres"), [match],
                                tva=line.get("tva"))
        return {"status": "pending", "store_id": store_id}

    # "exact" (référence déjà connue côté magasin) ou "new" (aucune
    # correspondance -> nouvel article) : les deux sont sûrs à appliquer sans
    # confirmation. La référence fournisseur est utilisée telle quelle pour un
    # nouvel article (même convention que l'import BDR Excel existant).
    dest_ref = (line.get("ref_art") or "").strip()
    if not dest_ref:
        return {"status": "error", "error": "ref_art manquant"}
    result = apply_new_line(con, registry, store_id, src_nopiece, src_noitem, dest_ref, line)
    result["store_id"] = store_id
    return result


def apply_new_line(con: sqlite3.Connection, registry, store_id: int, src_nopiece: str,
                   src_noitem: str, dest_ref: str, line: dict) -> dict:
    """Crée (ou rejoint) la réception pour UNE ligne dont la référence
    destinataire est déjà connue (rapprochement automatique 'exact'/'new'
    DANS process_line, ou résolution humaine d'une ligne 'pending' depuis le
    tableau de bord) — même chemin online/offline que tout le reste
    (submit_op).

    Si ce bon de livraison (src_nopiece) a DÉJÀ une réception créée via une
    autre de ses lignes, cette ligne la rejoint (item_add) au lieu d'en créer
    une seconde pour le même bon — c'est le cas où le fournisseur ajoute des
    articles à un BL déjà synchronisé."""
    qte = float(line.get("qte") or 0)
    prix = float(line.get("prix") or 0)
    bdr_line = _build_bdr_line(dest_ref, line)

    known_nopiece = fournisseur_known_dest_nopiece(con, store_id, src_nopiece)
    if not known_nopiece:
        # fournisseur_sync_state ne sait pas (encore) qu'une réception existe
        # déjà pour ce BL : peut arriver si sa toute première ligne a été
        # créée HORS LIGNE puis appliquée par l'agent sans jamais mettre à
        # jour l'état (rien ne revient le dire). Sans ce repli, cette ligne
        # créerait une SECONDE réception pour le même bon. Voir le docstring
        # du module (REFDOC) et fournisseur_dest_nopiece_by_refdoc.
        known_nopiece = fournisseur_dest_nopiece_by_refdoc(con, store_id, src_nopiece)
    if known_nopiece:
        import import_bon_reception as bdr  # type: ignore
        result = submit_op(con, registry, store_id, "item_add",
                           {"config": bdr.load_config(None), "nopiece": known_nopiece,
                            "lines": [bdr_line]})
        if result.get("status") == "applied":
            items = result.get("items") or []
            noitem = items[0]["noitem"] if items else ""
            fournisseur_sync_state_set(con, store_id, src_nopiece, src_noitem, dest_ref,
                                       known_nopiece, noitem, qte, prix)
        elif result.get("status") == "queued":
            # La pièce est déjà connue, mais le NOITEM de CETTE ligne ne le
            # sera qu'une fois la file appliquée par l'agent -> laissé vide
            # (cf. la vérification élargie dans process_line qui couvre aussi
            # ce cas, pas seulement dest_nopiece vide).
            fournisseur_sync_state_set(con, store_id, src_nopiece, src_noitem,
                                       dest_ref, known_nopiece, "", qte, prix)
        return result

    store = None
    if registry:
        try:
            store = registry.get(store_id)
        except Exception:  # noqa: BLE001
            # Mapping pointant vers un id absent de stores.json (magasin
            # renuméroté/supprimé depuis) : une erreur propre pour CETTE
            # ligne, pas une exception non rattrapée qui ferait planter tout
            # le lot (500 générique côté exe, aucune ligne du lot appliquée).
            return {"status": "error", "error": "Magasin inconnu (id=%s)." % store_id}
    online = False
    if store:
        from hub.write_back import is_reachable
        online = is_reachable(store.host, store.port)

    if online:
        from hub.write_back import write_bdr_result, WriteError
        import import_bon_reception as bdr  # type: ignore
        cfg = bdr.load_config(None)
        # REFDOC = NOPIECE du BL d'origine chez le père : voir le docstring
        # du module et fournisseur_dest_nopiece_by_refdoc pour la raison.
        cfg["refdoc"] = src_nopiece
        try:
            imp_result = write_bdr_result(store.connect_kwargs(), cfg, [bdr_line])
        except WriteError as exc:
            return {"status": "error", "error": str(exc)}
        item = imp_result["items"][0]
        fournisseur_sync_state_set(con, store_id, src_nopiece, src_noitem, dest_ref,
                                   imp_result["nopiece"], item["noitem"], qte, prix)
        return {"status": "applied", "ref_art": dest_ref, "nopiece": imp_result["nopiece"]}

    # Hors ligne : file d'attente normale (l'agent applique la création à sa
    # prochaine synchro) — le NOPIECE/NOITEM créé ne sera connu qu'à ce
    # moment-là, donc dest_nopiece reste vide pour l'instant (cf. la
    # vérification "pending_creation" dans process_line, qui évite une double
    # création tant qu'il n'est pas encore renseigné). Config par défaut
    # complète (comme le chemin en ligne) : un dict partiel ferait planter
    # Importer.__init__ (code_type_piece, default_famille... manquants).
    import import_bon_reception as bdr  # type: ignore
    cfg = bdr.load_config(None)
    cfg["refdoc"] = src_nopiece
    result = submit_op(con, registry, store_id, "bdr_import",
                       {"config": cfg, "lines": [bdr_line]})
    if result.get("status") == "queued":
        fournisseur_sync_state_set(con, store_id, src_nopiece, src_noitem,
                                   dest_ref, "", "", qte, prix)
    return result


def process_batch(con: sqlite3.Connection, registry, lines: list) -> list:
    """Une exception NON PRÉVUE sur une ligne (bug, donnée inattendue...) ne
    doit jamais faire échouer tout le lot avec un 500 générique — l'exe
    envoie potentiellement des dizaines de lignes par requête, la plupart
    n'ayant rien à voir avec celle qui pose problème."""
    results = []
    for line in lines:
        try:
            results.append(process_line(con, registry, line))
        except Exception as exc:  # noqa: BLE001
            results.append({"status": "error", "error": str(exc)})
    return results


def reconcile_article(con: sqlite3.Connection, registry, ref: str | None,
                      designation: str | None, days: int, father_lines: list) -> list:
    """Diagnostic manuel (onglet « Vérification article » de l'exe fournisseur)
    — jamais appelé par la synchro automatique, ne modifie RIEN.

    `ref`/`designation` : le même filtre article que l'exe a utilisé pour lire
    `father_lines` sur SON Firebird (mêmes clés que process_line : src_nopiece,
    code_tiers, ref_art, designation, qte...) — réutilisé tel quel côté magasin
    pour comparer sur le MÊME article, pas un article reconstruit à partir des
    lignes reçues (fragile si plusieurs références partagent une désignation).

    Regroupe father_lines par magasin destinataire (via fournisseur_mapping)
    et, pour chaque magasin JOIGNABLE, interroge sa base en direct :
      * bdr_reconcile (même moteur que l'aperçu BDR) pour savoir si la
        référence du père correspond à une référence DIFFÉRENTE côté magasin
        (rapprochement par nom) ou n'existe pas du tout côté magasin ;
      * reception_summary pour la quantité déjà reçue sur la même période,
        à comparer visuellement à la quantité livrée par le père.

    Toujours UNE ligne par magasin mappé (même à 0 côté père) pour un tableau
    à nombre de lignes stable, plus une ligne « non mappé » s'il existe des
    codes clients non mappés dans father_lines."""
    from hub.write_back import is_reachable, bdr_reconcile, reception_summary
    import datetime

    mapping = fournisseur_mapping(con)
    by_store: dict = {}
    unmapped_qte = 0.0
    for fl in father_lines or []:
        m = mapping.get(str(fl.get("code_tiers") or ""))
        if not m:
            unmapped_qte += float(fl.get("qte") or 0)
            continue
        sid = m["store_id"]
        entry = by_store.setdefault(sid, {"lines": [], "qte": 0.0})
        entry["lines"].append(fl)
        entry["qte"] += float(fl.get("qte") or 0)

    cutoff = datetime.datetime.now() - datetime.timedelta(days=days)
    report = []
    for store_id in sorted({m["store_id"] for m in mapping.values()}):
        store = None
        if registry:
            try:
                store = registry.get(store_id)
            except Exception:  # noqa: BLE001
                pass  # mapping pointant vers un id absent de stores.json -> ligne "hors ligne"
        row = {"store_id": store_id,
              "store_name": store.name if store else "Magasin %s" % store_id,
              "qte_pere": round(by_store.get(store_id, {}).get("qte", 0.0), 4),
              "online": False, "reception_qte": None, "matches": []}
        if store:
            row["online"] = is_reachable(store.host, store.port)
        if row["online"] and store:
            kw = store.connect_kwargs()
            lines_for_store = by_store.get(store_id, {}).get("lines", [])
            if lines_for_store:
                # Une entrée par référence distincte : pas la peine de
                # rappeler le rapprochement pour chaque ligne d'un même
                # article livré plusieurs fois sur la période.
                distinct = {}
                for fl in lines_for_store:
                    distinct.setdefault(fl.get("ref_art"), {
                        "ref_art": fl.get("ref_art"), "designation": fl.get("designation")})
                try:
                    row["matches"] = bdr_reconcile(kw, list(distinct.values()))
                except Exception as exc:  # noqa: BLE001
                    row["match_error"] = str(exc)
            try:
                summary = reception_summary(kw, "PC_AC_B", cutoff, ref, designation)
                row["reception_qte"] = summary["qte_total"]
            except Exception as exc:  # noqa: BLE001
                row["reception_error"] = str(exc)
        report.append(row)

    if unmapped_qte:
        report.append({"store_id": None, "store_name": "(code client non mappé)",
                       "qte_pere": round(unmapped_qte, 4), "online": None,
                       "reception_qte": None, "matches": []})
    return report
