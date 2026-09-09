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
  * La réception créée porte la MÊME DATE que le BL d'origine chez le père
    (date_piece, envoyée par l'exe), pas la date du jour de la synchro —
    sinon un rattrapage tardif (poste hors ligne, import d'historique) daterait
    des réceptions du jour de la synchro au lieu du jour de la livraison
    réelle, ce qui décale les dates entre les bases du père et des magasins.
  * Le NOPIECE du BL d'origine est stocké dans PIECE.REFDOC de la réception
    créée (côté magasin) : sert de repli pour retrouver une réception créée
    HORS LIGNE puis appliquée par l'agent sans jamais repasser par ici (voir
    fournisseur_dest_nopiece_by_refdoc) — sinon fournisseur_sync_state ne
    l'apprend jamais, bloquant toute édition ultérieure et risquant une
    réception en double si le fournisseur ajoute un article au même BL. Le
    repli est borné au type de pièce des réceptions (REFDOC est un champ
    texte libre que le personnel peut aussi remplir à la main sur SES PROPRES
    pièces) et exclut les pièces annulées.
  * process_batch tourne sous un verrou process global (_LOCK) : le hub Flask
    est multi-thread, et deux passages qui se chevauchent (relance manuelle
    pendant que la minuterie de l'exe se déclenche, par ex.) pourraient sinon
    tous les deux décider "pas de réception connue" pour la même ligne et
    créer deux réceptions. Les volumes réels (quelques centaines de
    lignes/jour) rendent ce verrou sans impact perceptible.
"""

from __future__ import annotations

import datetime
import os
import sqlite3
import sys
import threading

_VENDOR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "vendor")
_BDR_DIR = os.path.join(_VENDOR, "bdr")
if _BDR_DIR not in sys.path:
    sys.path.insert(0, _BDR_DIR)

from hub.central_db import (
    fournisseur_mapping, fournisseur_sync_state_get, fournisseur_sync_state_set,
    fournisseur_sync_state_clear, fournisseur_pending_add, fournisseur_known_dest_nopiece,
    fournisseur_dest_nopiece_by_refdoc, fournisseur_dest_noitem_by_ref,
    fournisseur_settings_get, fournisseur_store_settings_get,
    fournisseur_pending_creation_get, fournisseur_pending_creation_set,
    fournisseur_pending_creation_clear, pending_op_status, pending_op_append_line,
    log_immediate_op,
)
from hub.ops import submit_op

# Sérialise TOUT le traitement des lignes fournisseur sur ce process (voir la
# note "process_batch tourne sous un verrou" dans le docstring du module).
_LOCK = threading.Lock()


def _code_type_piece_reception(con: sqlite3.Connection) -> str:
    """Type de pièce des réceptions, tel que confirmé sur le tableau de
    bord — retombe sur le défaut générique d'import_bon_reception.py
    (PC_AC_B) tant qu'il n'a pas été confirmé sur ce déploiement."""
    return fournisseur_settings_get(con).get("code_type_piece_reception") or "PC_AC_B"


def _apply_store_settings(con: sqlite3.Connection, store_id: int, cfg: dict) -> dict:
    """Applique, s'ils ont été confirmés sur le tableau de bord : le type de
    pièce des réceptions, et le fournisseur (CODE_TIERS)/dépôt (CODE_DEPOT) à
    utiliser DANS ce magasin pour représenter le père comme tiers — sans ça,
    les réceptions créées n'ont ni fournisseur ni dépôt attaché côté
    Firebird. Vide par défaut : ne change rien tant que l'utilisateur ne l'a
    pas renseigné explicitement (un magasin à la fois)."""
    settings = fournisseur_settings_get(con)
    if settings.get("code_type_piece_reception"):
        cfg["code_type_piece"] = settings["code_type_piece_reception"]
    store_settings = fournisseur_store_settings_get(con, store_id)
    if store_settings.get("code_tiers"):
        cfg["code_tiers"] = store_settings["code_tiers"]
    if store_settings.get("code_depot"):
        cfg["code_depot"] = store_settings["code_depot"]
    return cfg


def _parse_date_piece(line: dict) -> datetime.datetime | None:
    """La date du BL d'origine (envoyée par l'exe en 'YYYY-MM-DD') — None si
    absente/invalide, l'appelant retombe alors sur la date du jour (comme
    avant que cette fonctionnalité n'existe)."""
    raw = line.get("date_piece")
    if not raw:
        return None
    try:
        return datetime.datetime.strptime(str(raw)[:10], "%Y-%m-%d")
    except ValueError:
        return None


def _match_line(con: sqlite3.Connection, store_id: int, line: dict) -> dict:
    """Rapproche une ligne fournisseur avec le catalogue MIROIR (central.db)
    du magasin destinataire, via le même moteur que l'import BDR classique
    (best_match_for_line) — juste borné à un magasin via le paramètre
    extra_where de build_article_index (ajouté pour cet usage).

    Code-barres d'abord : un article déjà vendu par le magasin sous une
    référence DIFFÉRENTE mais le MÊME code-barres est aussi sûr qu'une
    référence identique (même produit, sans ambiguïté) — pas la peine de
    passer par le rapprochement approximatif par désignation pour ça."""
    import import_bon_reception as bdr  # type: ignore
    code_barres = (line.get("code_barres") or "").strip()
    if code_barres:
        row = con.execute(
            "SELECT ref_art FROM equiv_cbarres WHERE store_id=? AND code_barres=?",
            (store_id, code_barres)).fetchone()
        if row:
            return {"ref_exists": False, "match_ref": row["ref_art"], "match_designation": None,
                   "match_score": 1.0, "match_prix_achat": None, "status": "exact"}
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
    prix, tva, code_barres, date_piece}. Renvoie {"status": "applied"|
    "queued"|"unchanged"|"pending"|"ignored"|"skipped"|"error", ...}."""
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
    code_type_piece = _code_type_piece_reception(con)

    state = fournisseur_sync_state_get(con, store_id, src_nopiece, src_noitem)
    if state:
        dest_nopiece = state["dest_nopiece"]
        dest_noitem = state["dest_noitem"]
        if not dest_nopiece or not dest_noitem:
            # La création a pu passer par la file (magasin hors ligne au
            # moment du dernier passage) puis être appliquée par l'agent SANS
            # que fournisseur_sync_state en soit jamais informé (rien ne
            # revient le dire) — on cherche la réception via REFDOC avant
            # d'abandonner. Se corrige tout seul dès que l'agent a synchronisé
            # le miroir, pas besoin d'attendre un changement supplémentaire.
            dest_nopiece = dest_nopiece or fournisseur_dest_nopiece_by_refdoc(
                con, store_id, src_nopiece, code_type_piece)
            if dest_nopiece and not dest_noitem:
                dest_noitem = fournisseur_dest_noitem_by_ref(
                    con, store_id, dest_nopiece, state["dest_ref_art"])
        if dest_nopiece and dest_noitem:
            # IMPORTANT : ce raccourci ne doit s'appliquer QUE quand la
            # destination est réellement connue — sinon une création encore
            # EN FILE (dest_nopiece vide, qte/prix identiques à ceux déjà
            # enregistrés lors de la mise en file, le cas normal) court-
            # circuiterait ici et ne passerait JAMAIS par la vérification
            # d'échec ci-dessous, la laissant bloquée pour toujours.
            if state["last_qte"] == qte and state["last_prix"] == prix:
                return {"status": "unchanged", "store_id": store_id}
            edits = [{"nopiece": dest_nopiece, "noitem": dest_noitem,
                     "qte": qte, "prix": prix, "maj_prix_achat": True}]
            result = submit_op(con, registry, store_id, "item_edit", {"edits": edits})
            if result.get("status") in ("applied", "queued"):
                fournisseur_sync_state_set(con, store_id, src_nopiece, src_noitem,
                                           state["dest_ref_art"], dest_nopiece,
                                           dest_noitem, qte, prix)
            result["store_id"] = store_id
            return result
        # Toujours pas de pièce/ligne connue : la création est-elle encore
        # légitimement en file, ou a-t-elle ÉCHOUÉ (auquel cas la marchandise
        # n'arrivera JAMAIS côté magasin sans qu'on retraite cette ligne
        # depuis zéro — sinon elle resterait signalée "en attente" pour
        # toujours alors que rien n'a réellement été livré) ? Vérifié même si
        # qte/prix n'ont PAS changé depuis la mise en file (le cas normal) :
        # last_qte/last_prix reflètent déjà les valeurs envoyées à la file,
        # donc un simple test d'égalité les manquerait toujours.
        op_id = state["op_id"] if "op_id" in state.keys() else None
        if op_id and pending_op_status(con, op_id) == "failed":
            fournisseur_sync_state_clear(con, store_id, src_nopiece, src_noitem)
            fournisseur_pending_creation_clear(con, store_id, src_nopiece)
            # tombe dans le traitement "nouvelle ligne" ci-dessous
        else:
            return {"status": "pending_creation", "store_id": store_id}

    match = _match_line(con, store_id, line)
    if match["status"] == "matched":
        # Rapprochement par NOM seulement (pas de référence/code-barres
        # identique) : jamais automatique, comme la correspondance manuelle
        # ailleurs dans l'appli — un humain doit confirmer sur le dashboard.
        status = fournisseur_pending_add(con, store_id, src_nopiece, src_noitem,
                                         line.get("ref_art"), line.get("designation"),
                                         qte, prix, line.get("code_barres"), [match],
                                         tva=line.get("tva"))
        # 'ignored' si un humain a déjà écarté cette ligne (sinon l'exe
        # afficherait "à valider" indéfiniment pour une ligne déjà traitée).
        return {"status": status if status == "ignored" else "pending", "store_id": store_id}

    # "exact" (référence déjà connue côté magasin) ou "new" (aucune
    # correspondance -> nouvel article) : les deux sont sûrs à appliquer sans
    # confirmation. La référence fournisseur est utilisée telle quelle pour un
    # nouvel article (même convention que l'import BDR Excel existant).
    dest_ref = (match.get("match_ref") or line.get("ref_art") or "").strip()
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
    autre de ses lignes — ou une création encore EN FILE pour ce même bon —
    cette ligne la rejoint (item_add, ou fusion dans l'op déjà en file) au
    lieu d'en créer une seconde pour le même bon. C'est le cas où le
    fournisseur ajoute des articles à un BL déjà synchronisé, OU envoie un BL
    de plusieurs lignes pendant que le magasin est hors ligne."""
    qte = float(line.get("qte") or 0)
    prix = float(line.get("prix") or 0)
    date_piece = _parse_date_piece(line)
    date_piece_iso = date_piece.strftime("%Y-%m-%d") if date_piece else None
    bdr_line = _build_bdr_line(dest_ref, line)
    code_type_piece = _code_type_piece_reception(con)

    known_nopiece = fournisseur_known_dest_nopiece(con, store_id, src_nopiece)
    if not known_nopiece:
        # fournisseur_sync_state ne sait pas (encore) qu'une réception existe
        # déjà pour ce BL : peut arriver si sa toute première ligne a été
        # créée HORS LIGNE puis appliquée par l'agent sans jamais mettre à
        # jour l'état (rien ne revient le dire). Sans ce repli, cette ligne
        # créerait une SECONDE réception pour le même bon. Voir le docstring
        # du module (REFDOC) et fournisseur_dest_nopiece_by_refdoc.
        known_nopiece = fournisseur_dest_nopiece_by_refdoc(con, store_id, src_nopiece,
                                                            code_type_piece)
    if known_nopiece:
        import import_bon_reception as bdr  # type: ignore
        result = submit_op(con, registry, store_id, "item_add",
                           {"config": bdr.load_config(None), "nopiece": known_nopiece,
                            "lines": [bdr_line], "date_piece": date_piece_iso})
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
                                       dest_ref, known_nopiece, "", qte, prix,
                                       op_id=result.get("op_id"))
        return result

    # Pas de pièce connue DU TOUT (ni fournisseur_sync_state, ni REFDOC) —
    # mais une création pour ce MÊME bon peut déjà être en file (une ligne
    # sœur traitée juste avant, magasin hors ligne) : la rejoindre, sinon
    # chaque ligne d'un BL envoyé hors ligne créerait sa PROPRE réception à
    # une ligne au lieu d'une seule réception pour tout le bon.
    pending_op_id = fournisseur_pending_creation_get(con, store_id, src_nopiece)
    if pending_op_id and pending_op_append_line(con, pending_op_id, bdr_line):
        fournisseur_sync_state_set(con, store_id, src_nopiece, src_noitem,
                                   dest_ref, "", "", qte, prix, op_id=pending_op_id)
        return {"status": "queued", "op_id": pending_op_id}

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
        cfg = _apply_store_settings(con, store_id, cfg)
        try:
            imp_result = write_bdr_result(store.connect_kwargs(), cfg, [bdr_line],
                                          date_piece=date_piece)
        except WriteError as exc:
            return {"status": "error", "error": str(exc)}
        item = imp_result["items"][0]
        fournisseur_sync_state_set(con, store_id, src_nopiece, src_noitem, dest_ref,
                                   imp_result["nopiece"], item["noitem"], qte, prix)
        # write_bdr_result écrit directement (hors submit_op) : sans ceci,
        # cette création — la plus importante de toutes — n'apparaîtrait
        # JAMAIS dans /historique, contrairement à ses éditions ultérieures
        # (item_edit/item_add, qui passent par submit_op).
        log_immediate_op(con, store_id, "bdr_import",
                         {"config": cfg, "lines": [bdr_line]}, "applied")
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
    cfg = _apply_store_settings(con, store_id, cfg)
    result = submit_op(con, registry, store_id, "bdr_import",
                       {"config": cfg, "lines": [bdr_line], "date_piece": date_piece_iso})
    if result.get("status") == "queued":
        fournisseur_sync_state_set(con, store_id, src_nopiece, src_noitem,
                                   dest_ref, "", "", qte, prix, op_id=result.get("op_id"))
        fournisseur_pending_creation_set(con, store_id, src_nopiece, result["op_id"])
    return result


def process_batch(con: sqlite3.Connection, registry, lines: list) -> list:
    """Une exception NON PRÉVUE sur une ligne (bug, donnée inattendue...) ne
    doit jamais faire échouer tout le lot avec un 500 générique — l'exe
    envoie potentiellement des dizaines de lignes par requête, la plupart
    n'ayant rien à voir avec celle qui pose problème.

    Sous _LOCK (voir le docstring du module) : sérialise le traitement de
    TOUS les lots fournisseur sur ce process, pour qu'un passage manuel et la
    minuterie automatique qui se chevauchent ne puissent jamais tous les deux
    décider "pas de réception connue" pour la même ligne."""
    results = []
    with _LOCK:
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
    Le stock ACTUEL (positif ou négatif) vient lui du miroir (stock_snapshot,
    déjà tenu à jour par la synchro régulière de l'agent) — pas besoin que le
    magasin soit joignable pour l'afficher, contrairement au rapprochement/
    aux réceptions ci-dessus.

    Toujours UNE ligne par magasin mappé (même à 0 côté père) pour un tableau
    à nombre de lignes stable, plus une ligne « non mappé » s'il existe des
    codes clients non mappés dans father_lines."""
    from hub.write_back import is_reachable, bdr_reconcile, reception_summary
    from hub.central_db import stock_by_ref

    code_type_piece_reception = _code_type_piece_reception(con)
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
              "online": False, "reception_qte": None, "matches": [], "stock": None}
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
                summary = reception_summary(kw, code_type_piece_reception, cutoff, ref, designation)
                row["reception_qte"] = summary["qte_total"]
            except Exception as exc:  # noqa: BLE001
                row["reception_error"] = str(exc)

        # Stock actuel (miroir, pas besoin d'être en ligne) : la référence à
        # regarder est celle du MAGASIN, pas forcément celle du père —
        # d'abord la correspondance trouvée ci-dessus (exact/matched, celle
        # que ce magasin utilise réellement), sinon la référence recherchée
        # telle quelle si elle a été fournie (cas le plus courant : recherche
        # par référence exacte). Rien à afficher si on n'a ni l'un ni l'autre
        # (recherche par désignation seule, magasin injoignable pour le
        # rapprochement) — pas de référence sûre à interroger.
        m0 = row["matches"][0] if row["matches"] else None
        stock_ref = (m0.get("match_ref") if m0 and m0.get("status") in ("exact", "matched")
                    else None) or ref
        if store and stock_ref:
            row["stock"] = stock_by_ref(con, store_id, stock_ref)

        report.append(row)

    if unmapped_qte:
        report.append({"store_id": None, "store_name": "(code client non mappé)",
                       "qte_pere": round(unmapped_qte, 4), "online": None,
                       "reception_qte": None, "matches": [], "stock": None})
    return report
