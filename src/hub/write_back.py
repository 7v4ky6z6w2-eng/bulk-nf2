"""Écriture directe sur un magasin EN LIGNE via Tailscale.

Utilisé par la page bureau BDR / prix quand le magasin cible est joignable.
Si le magasin est hors ligne, l'appelant enqueue_op() à la place.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import sys
import tempfile
import threading

_VENDOR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "vendor")
_BDR_DIR = os.path.join(_VENDOR, "bdr")
_EDITOR_DIR = os.path.join(_VENDOR, "editor")
for _d in (_BDR_DIR, _EDITOR_DIR):
    if _d not in sys.path:
        sys.path.insert(0, _d)

# bdr.main() lit ses arguments via argparse sur sys.argv (global au process).
# Le hub Flask tourne en threaded=True : sans ce verrou, deux imports BDR
# simultanés (bureau + mobile, ou deux téléphones) peuvent lire les chemins de
# fichiers temporaires l'un de l'autre.
_BDR_LOCK = threading.Lock()


class WriteError(Exception):
    pass


def write_bdr(connect_kwargs: dict, config: dict, lines: list, date_piece=None) -> None:
    """Importe un BDR directement sur le magasin via ses connect_kwargs (Tailscale).

    `date_piece` (optionnel, "YYYY-MM-DD") : sert quand la synchro fournisseur
    passe par CE chemin plutôt que write_bdr_result — ne devrait normalement
    arriver que dans une fenêtre de course très étroite (magasin détecté hors
    ligne par apply_new_line, mais de nouveau joignable pile au moment où
    submit_op vérifie à son tour) ; les autres appelants (BDR bureau/mobile)
    ne le passent jamais, la date du jour reste leur comportement normal."""
    import import_bon_reception as bdr  # type: ignore

    cfg = dict(config)
    cfg.update({
        "host": connect_kwargs.get("host", "localhost"),
        "port": connect_kwargs.get("port", 3050),
        "database": connect_kwargs["database"],
        "user": connect_kwargs.get("user", "SYSDBA"),
        "password": connect_kwargs.get("password", ""),
        "charset": connect_kwargs.get("charset", "WIN1256"),
    })
    if not lines:
        raise WriteError("BDR sans lignes.")

    tmpdir = tempfile.mkdtemp(prefix="bdr_direct_")
    cfg_path = os.path.join(tmpdir, "config.json")
    lines_path = os.path.join(tmpdir, "lines.json")
    with open(cfg_path, "w", encoding="utf-8") as fh:
        json.dump(cfg, fh, ensure_ascii=False)
    with open(lines_path, "w", encoding="utf-8") as fh:
        json.dump(lines, fh, ensure_ascii=False)

    argv = ["import_bon_reception", "--config", cfg_path, "--lines", lines_path]
    if date_piece:
        argv += ["--date", str(date_piece)[:10]]
    buf = io.StringIO()
    with _BDR_LOCK:  # sys.argv est global au process : sérialise les imports BDR
        old_argv = sys.argv
        try:
            sys.argv = argv
            with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
                bdr.main()
        except SystemExit as exc:
            if exc.code not in (0, None):
                raise WriteError("BDR échoué : %s" % (buf.getvalue().strip()[-400:] or exc.code)) from exc
        except Exception as exc:  # noqa: BLE001
            raise WriteError("BDR échoué : %s" % exc) from exc
        finally:
            sys.argv = old_argv


def _bdr_cfg(connect_kwargs: dict) -> dict:
    return {
        "host": connect_kwargs.get("host", "localhost"),
        "port": connect_kwargs.get("port", 3050),
        "database": connect_kwargs["database"],
        "user": connect_kwargs.get("user", "SYSDBA"),
        "password": connect_kwargs.get("password", ""),
        "charset": connect_kwargs.get("charset", "WIN1256"),
    }


def bdr_tiers(connect_kwargs: dict) -> dict:
    """Fournisseurs/dépôts du magasin cible (connexion Firebird live — le
    magasin doit être en ligne). Sert à peupler le sélecteur fournisseur de
    l'aperçu BDR avant confirmation."""
    import import_bon_reception as bdr  # type: ignore
    return bdr.list_tiers(_bdr_cfg(connect_kwargs))


def bdr_reconcile(connect_kwargs: dict, lines: list) -> list:
    """Rapprochement de chaque ligne d'un Excel/PDF fournisseur avec les
    articles EXISTANTS du magasin cible (connexion live) — voir
    import_bon_reception.best_match_for_line. Ne modifie rien : sert à
    afficher les correspondances dans l'aperçu avant confirmation."""
    import import_bon_reception as bdr  # type: ignore
    return bdr.reconcile_lines(_bdr_cfg(connect_kwargs), lines)


def reception_summary(connect_kwargs: dict, code_type_piece: str, cutoff,
                      ref: str | None, designation: str | None) -> dict:
    """Total déjà reçu (pièces de type `code_type_piece`, ex PC_AC_B) pour un
    article, depuis `cutoff` — connexion live, LECTURE SEULE. Sert à l'onglet
    « Vérification article » de la synchro fournisseur : comparer ce qu'un
    magasin a déjà reçu à ce que le père a livré sur la même période, pour
    repérer ce qui a déjà été saisi à la main avant la synchro automatique."""
    import fdb  # type: ignore
    clauses, params = [], []
    if ref:
        clauses.append("i.REF_ART = ?")
        params.append(ref)
    if designation:
        clauses.append("a.DESIGNATION CONTAINING ?")
        params.append(designation)
    if not clauses:
        return {"qte_total": 0.0, "lines": []}
    con = fdb.connect(**connect_kwargs)
    try:
        cur = con.cursor()
        cur.execute(
            "SELECT p.NOPIECE, p.DATEPIECE, i.REF_ART, a.DESIGNATION, i.QTE, i.PRIXHT "
            "FROM PIECE p JOIN ITEM i ON i.NOPIECE = p.NOPIECE "
            "LEFT JOIN ARTICLE a ON a.REF_ART = i.REF_ART "
            "WHERE p.CODE_TYPE_PIECE = ? AND p.DATEPIECE >= ? "
            # ANNULEE=1 = réception ACTIVE, ANNULEE=0 = annulée (convention
            # inversée pour ce type de pièce, voir mode_cancel_piece) : sans
            # cette exclusion, une réception annulée gonflait quand même la
            # quantité "déjà reçue" affichée dans Vérification article.
            "AND (p.ANNULEE IS NULL OR p.ANNULEE <> 0) AND (%s) "
            "ORDER BY p.DATEPIECE" % " OR ".join(clauses),
            [code_type_piece, cutoff] + params)
        rows = cur.fetchall()
    finally:
        con.close()
    lines = [{"nopiece": str(r[0]), "date": str(r[1])[:10], "ref_art": r[2],
             "designation": r[3], "qte": float(r[4] or 0), "prix": float(r[5] or 0)}
            for r in rows]
    return {"qte_total": round(sum(l["qte"] for l in lines), 4), "lines": lines}


def write_bdr_result(connect_kwargs: dict, config: dict, lines: list, date_piece=None) -> dict:
    """Comme write_bdr, mais appelle run_import DIRECTEMENT (pas de
    fichiers temporaires ni de sys.argv/bdr.main()) et renvoie le résultat
    structuré (NOPIECE/NOITEM créés, entre autres) — nécessaire pour la
    synchro fournisseur, qui doit retrouver la ligne créée si le fournisseur
    modifie prix/qté après coup.

    Prend le MÊME verrou que write_bdr (_BDR_LOCK), pas pour sys.argv cette
    fois mais parce que Importer.next_base numérote NOPIECE/NOITEM en lisant
    MAX(...) dans SA PROPRE transaction : deux écritures concurrentes sur le
    MÊME magasin (fournisseur + import bureau/mobile, ou deux lots
    fournisseur qui se chevauchent) peuvent lire le même MAX avant que l'une
    ou l'autre ne valide, et obtenir le même NOPIECE — un seul verrou par
    process pour toute allocation de numéro suffit à l'empêcher (volumes
    réels : quelques centaines de lignes/jour, le coût de sérialisation est
    négligeable)."""
    import import_bon_reception as bdr  # type: ignore
    if not lines:
        raise WriteError("BDR sans lignes.")
    cfg = dict(config)
    cfg.update({
        "host": connect_kwargs.get("host", "localhost"),
        "port": connect_kwargs.get("port", 3050),
        "database": connect_kwargs["database"],
        "user": connect_kwargs.get("user", "SYSDBA"),
        "password": connect_kwargs.get("password", ""),
        "charset": connect_kwargs.get("charset", "WIN1256"),
    })
    try:
        with _BDR_LOCK:
            return bdr.run_import(cfg, lines, date_piece=date_piece)
    except Exception as exc:  # noqa: BLE001
        raise WriteError("Import BDR échoué : %s" % exc) from exc


def write_items_added(connect_kwargs: dict, config: dict, nopiece: str, lines: list,
                      date_piece=None) -> dict:
    """Ajoute des lignes à une PIECE déjà existante (nopiece connu), sans en
    créer une seconde — synchro fournisseur : le fournisseur ajoute des
    articles à un bon de livraison déjà synchronisé, ces articles rejoignent
    la réception déjà créée. Voir import_bon_reception.add_items. Même
    verrou que write_bdr_result, même raison (numérotation NOITEM)."""
    import import_bon_reception as bdr  # type: ignore
    if not lines:
        raise WriteError("Aucune ligne à ajouter.")
    cfg = dict(config)
    cfg.update({
        "host": connect_kwargs.get("host", "localhost"),
        "port": connect_kwargs.get("port", 3050),
        "database": connect_kwargs["database"],
        "user": connect_kwargs.get("user", "SYSDBA"),
        "password": connect_kwargs.get("password", ""),
        "charset": connect_kwargs.get("charset", "WIN1256"),
    })
    try:
        with _BDR_LOCK:
            return bdr.add_items(cfg, nopiece, lines, date_piece=date_piece)
    except Exception as exc:  # noqa: BLE001
        raise WriteError("Ajout de ligne(s) échoué : %s" % exc) from exc


def write_cancel_piece(connect_kwargs: dict, nopiece: str) -> dict:
    """Annule une pièce (ANNULEE=0, reprise native du stock) — sert au
    bouton « Annuler cette réception » du tableau de bord (historique
    synchro fournisseur). Voir import_bon_reception.cancel_piece."""
    import import_bon_reception as bdr  # type: ignore
    if not nopiece:
        raise WriteError("Annulation sans NOPIECE.")
    try:
        return bdr.cancel_piece(_bdr_cfg(connect_kwargs), nopiece)
    except Exception as exc:  # noqa: BLE001
        raise WriteError("Annulation échouée : %s" % exc) from exc


def write_item_edit(connect_kwargs: dict, edits: list) -> None:
    """Modifie en place des lignes ITEM déjà créées (nopiece/noitem connus) —
    quantité/prix d'une réception déjà importée qui change ensuite (synchro
    fournisseur). Voir import_bon_reception.edit_items pour la justification
    de sécurité vis-à-vis du stock (calculé à la volée, pas un compteur)."""
    import import_bon_reception as bdr  # type: ignore
    if not edits:
        raise WriteError("Aucune modification à appliquer.")
    try:
        bdr.edit_items(_bdr_cfg(connect_kwargs), edits)
    except Exception as exc:  # noqa: BLE001
        raise WriteError("Modification de ligne échouée : %s" % exc) from exc


def write_prices(connect_kwargs: dict, changes: list) -> None:
    """Met à jour les prix directement sur le magasin (host = IP Tailscale ou localhost)."""
    import article_db  # type: ignore

    cfg = {
        "host": connect_kwargs.get("host", "localhost"),
        "port": connect_kwargs.get("port", 3050),
        "database": connect_kwargs["database"],
        "user": connect_kwargs.get("user", "SYSDBA"),
        "password": connect_kwargs.get("password", ""),
        "charset": connect_kwargs.get("charset", "WIN1256"),
        "table": "ARTICLE",
    }
    if not changes:
        raise WriteError("Aucun changement de prix.")

    repo = article_db.ArticleRepository(cfg)
    try:
        repo.connect()
        repo.update_rows(changes)
        repo.commit()
    except article_db.DBError as exc:
        with contextlib.suppress(Exception):
            repo.rollback()
        raise WriteError(str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        with contextlib.suppress(Exception):
            repo.rollback()
        raise WriteError("Mise à jour prix échouée : %s" % exc) from exc
    finally:
        repo.close()


def write_barcode_ops(connect_kwargs: dict, ops: list) -> None:
    """Applique des ajouts/suppressions de codes-barres dans EQUIV_CBARRES.

    N'écrit QUE dans EQUIV_CBARRES (la table affichée par Netfact2) : ni
    ARTICLE.CODE_BARRE(35) ni ARTICLE.CODE_BARRES(60) ne sont touchés.
      * action 'add'    → add_barcode_equiv (logique vendorisée, générateur,
                          respect de l'unicité, idempotent) ;
      * action 'remove' → DELETE de la ligne (ref_art, code_barres).
    """
    import import_bon_reception as bdr  # type: ignore

    # load_config(None) donne la config par defaut complete (dont
    # code_type_piece, lu par Importer.__init__ -> _load_type_coeffs) : un
    # dict minimal host/port/database/user/password/charset ne suffit pas,
    # Importer(con, cfg) leve un KeyError sinon (aucune config cote appelant
    # pour cette operation, contrairement au BDR qui envoie la sienne).
    cfg = bdr.load_config(None)
    cfg.update({
        "host": connect_kwargs.get("host", "localhost"),
        "port": connect_kwargs.get("port", 3050),
        "database": connect_kwargs["database"],
        "user": connect_kwargs.get("user", "SYSDBA"),
        "password": connect_kwargs.get("password", ""),
        "charset": connect_kwargs.get("charset", "WIN1256"),
    })
    if not ops:
        raise WriteError("Aucune opération de code-barres.")

    con = None
    try:
        con = bdr.connect(cfg)
        imp = bdr.Importer(con, cfg)
        cur = con.cursor()
        for op in ops:
            ref = (op.get("ref_art") or "").strip()
            bc = (op.get("barcode") or "").strip()
            if not ref or not bc:
                continue
            if op.get("action") == "remove":
                cur.execute("DELETE FROM EQUIV_CBARRES WHERE REF_ART = ? AND CODE_BARRES = ?",
                            (ref, bc))
            else:
                imp.add_barcode_equiv(ref, bc)   # ajoute dans EQUIV_CBARRES
        con.commit()
    except Exception as exc:  # noqa: BLE001
        if con is not None:
            with contextlib.suppress(Exception):
                con.rollback()
        raise WriteError("Écriture code-barres échouée : %s" % exc) from exc
    finally:
        with contextlib.suppress(Exception):
            con.close()


def is_reachable(host: str, port: int = 3050, timeout: float = 3.0) -> bool:
    """Teste rapidement si le serveur Firebird du magasin est joignable."""
    import socket
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False
