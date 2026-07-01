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


def write_bdr(connect_kwargs: dict, config: dict, lines: list) -> None:
    """Importe un BDR directement sur le magasin via ses connect_kwargs (Tailscale)."""
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

    cfg = {
        "host": connect_kwargs.get("host", "localhost"),
        "port": connect_kwargs.get("port", 3050),
        "database": connect_kwargs["database"],
        "user": connect_kwargs.get("user", "SYSDBA"),
        "password": connect_kwargs.get("password", ""),
        "charset": connect_kwargs.get("charset", "WIN1256"),
    }
    if not ops:
        raise WriteError("Aucune opération de code-barres.")

    con = bdr.connect(cfg)
    imp = bdr.Importer(con, cfg)
    cur = con.cursor()
    try:
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
