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
    fournisseur_pending_add,
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
    return {
        "ref_art": ref_art,
        "designation": line.get("designation") or ref_art,
        "qte": float(line.get("qte") or 0),
        "prix": float(line.get("prix") or 0),
        "tva": line.get("tva", 19),
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
        if not state["dest_nopiece"]:
            # La création initiale est encore en file (magasin hors ligne au
            # moment du premier passage) : pas de ligne à éditer pour l'instant,
            # elle sera créée avec les valeurs déjà en file — on ne peut pas
            # encore répercuter ce changement. Cas rare, se corrigera au
            # prochain passage une fois la création appliquée.
            return {"status": "pending_creation", "store_id": store_id}
        edits = [{"nopiece": state["dest_nopiece"], "noitem": state["dest_noitem"],
                 "qte": qte, "prix": prix, "maj_prix_achat": True}]
        result = submit_op(con, registry, store_id, "item_edit", {"edits": edits})
        if result.get("status") in ("applied", "queued"):
            fournisseur_sync_state_set(con, store_id, src_nopiece, src_noitem,
                                       state["dest_ref_art"], state["dest_nopiece"],
                                       state["dest_noitem"], qte, prix)
        result["store_id"] = store_id
        return result

    match = _match_line(con, store_id, line)
    if match["status"] == "matched":
        # Rapprochement par NOM seulement (pas de référence/code-barres
        # identique) : jamais automatique, comme la correspondance manuelle
        # ailleurs dans l'appli — un humain doit confirmer sur le dashboard.
        fournisseur_pending_add(con, store_id, src_nopiece, src_noitem,
                                line.get("ref_art"), line.get("designation"),
                                qte, prix, line.get("code_barres"), [match])
        return {"status": "pending", "store_id": store_id}

    # "exact" (référence déjà connue côté magasin) ou "new" (aucune
    # correspondance -> nouvel article) : les deux sont sûrs à appliquer sans
    # confirmation. La référence fournisseur est utilisée telle quelle pour un
    # nouvel article (même convention que l'import BDR Excel existant).
    dest_ref = (line.get("ref_art") or "").strip()
    if not dest_ref:
        return {"status": "error", "error": "ref_art manquant"}
    bdr_line = _build_bdr_line(dest_ref, line)

    store = registry.get(store_id) if registry else None
    online = False
    if store:
        from hub.write_back import is_reachable
        online = is_reachable(store.host, store.port)

    if online:
        from hub.write_back import write_bdr_result, WriteError
        import import_bon_reception as bdr  # type: ignore
        try:
            imp_result = write_bdr_result(store.connect_kwargs(), bdr.load_config(None), [bdr_line])
        except WriteError as exc:
            return {"status": "error", "error": str(exc), "store_id": store_id}
        item = imp_result["items"][0]
        fournisseur_sync_state_set(con, store_id, src_nopiece, src_noitem, dest_ref,
                                   imp_result["nopiece"], item["noitem"], qte, prix)
        return {"status": "applied", "store_id": store_id, "ref_art": dest_ref,
               "nopiece": imp_result["nopiece"]}

    # Hors ligne : file d'attente normale (l'agent applique la création à sa
    # prochaine synchro) — le NOPIECE/NOITEM créé ne sera connu qu'à ce
    # moment-là, donc dest_nopiece reste vide pour l'instant (cf. la
    # vérification "pending_creation" plus haut, qui évite une double
    # création tant qu'il n'est pas encore renseigné). Config par défaut
    # complète (comme le chemin en ligne) : un dict partiel ferait planter
    # Importer.__init__ (code_type_piece, default_famille... manquants).
    import import_bon_reception as bdr  # type: ignore
    result = submit_op(con, registry, store_id, "bdr_import",
                       {"config": bdr.load_config(None), "lines": [bdr_line]})
    if result.get("status") == "queued":
        fournisseur_sync_state_set(con, store_id, src_nopiece, src_noitem,
                                   dest_ref, "", "", qte, prix)
    result["store_id"] = store_id
    return result


def process_batch(con: sqlite3.Connection, registry, lines: list) -> list:
    return [process_line(con, registry, line) for line in lines]
