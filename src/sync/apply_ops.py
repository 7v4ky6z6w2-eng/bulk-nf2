"""Application locale des opérations en file d'attente (BDR / prix).

Tourne DANS l'agent, sur le poste du magasin concerné. Récupère les pending_ops
du hub, les applique à la base Firebird LOCALE via le code primenf vendorisé,
puis acquitte (done/failed) au hub. Le hub envoie ensuite la notification.

  * bdr_import   : payload = {"config": {...}, "lines": [...]}.
                   On écrit deux fichiers temporaires et on appelle le main()
                   de l'outil BDR en mode --config --lines (logique identique au
                   CLI : PC_AC_B, générateurs NEXTPIECE/NEXTITEM, ANNULEE=1…).
  * price_update : payload = {"changes": [{"ref0":..., "values":{...}}], ...}.
                   On utilise ArticleRepository.update_rows + commit.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import sys
import tempfile

# Rendre les modules vendor importables (ils utilisent des imports frères).
_VENDOR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "vendor")
_BDR_DIR = os.path.join(_VENDOR, "bdr")
_EDITOR_DIR = os.path.join(_VENDOR, "editor")
for _d in (_BDR_DIR, _EDITOR_DIR):
    if _d not in sys.path:
        sys.path.insert(0, _d)


class ApplyError(Exception):
    pass


def apply_op(op: dict, connect_kwargs: dict) -> None:
    """Applique une opération localement. Lève ApplyError en cas d'échec."""
    op_type = op.get("op_type")
    payload = op.get("payload") or {}
    if op_type == "bdr_import":
        _apply_bdr(payload, connect_kwargs)
    elif op_type == "price_update":
        _apply_price(payload, connect_kwargs)
    elif op_type == "barcode_ops":
        _apply_barcode(payload, connect_kwargs)
    else:
        raise ApplyError("Type d'opération inconnu : %s" % op_type)


def _apply_barcode(payload: dict, connect_kwargs: dict) -> None:
    """Applique des ajouts/suppressions de codes-barres (EQUIV_CBARRES)."""
    import import_bon_reception as bdr  # type: ignore

    # load_config(None) donne la config par defaut complete (dont
    # code_type_piece, lu par Importer.__init__ -> _load_type_coeffs) : un
    # dict minimal host/port/database/user/password/charset ne suffit pas,
    # Importer(con, cfg) leve un KeyError sinon.
    cfg = bdr.load_config(None)
    cfg.update({
        "host": "localhost",
        "port": connect_kwargs.get("port", 3050),
        "database": connect_kwargs["database"],
        "user": connect_kwargs.get("user", "SYSDBA"),
        "password": connect_kwargs.get("password", ""),
        "charset": connect_kwargs.get("charset", "WIN1256"),
    })
    ops = payload.get("ops") or []
    if not ops:
        raise ApplyError("Aucune opération de code-barres.")

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
                imp.add_barcode_equiv(ref, bc)
        con.commit()
    except Exception as exc:  # noqa: BLE001
        if con is not None:
            with contextlib.suppress(Exception):
                con.rollback()
        raise ApplyError("Écriture code-barres échouée : %s" % exc) from exc
    finally:
        with contextlib.suppress(Exception):
            con.close()


# --------------------------------------------------------------------------- #
def _apply_bdr(payload: dict, connect_kwargs: dict) -> None:
    import import_bon_reception as bdr  # type: ignore

    cfg = dict(payload.get("config") or {})
    # Forcer la connexion sur la base LOCALE (le magasin applique chez lui).
    cfg.update({
        "host": "localhost",
        "port": connect_kwargs.get("port", 3050),
        "database": connect_kwargs["database"],
        "user": connect_kwargs.get("user", "SYSDBA"),
        "password": connect_kwargs.get("password", ""),
        "charset": connect_kwargs.get("charset", "WIN1256"),
    })
    lines = payload.get("lines") or []
    if not lines:
        raise ApplyError("BDR sans lignes.")

    tmpdir = tempfile.mkdtemp(prefix="bdr_op_")
    cfg_path = os.path.join(tmpdir, "config.json")
    lines_path = os.path.join(tmpdir, "lines.json")
    with open(cfg_path, "w", encoding="utf-8") as fh:
        json.dump(cfg, fh, ensure_ascii=False)
    with open(lines_path, "w", encoding="utf-8") as fh:
        json.dump(lines, fh, ensure_ascii=False)

    argv = ["import_bon_reception", "--config", cfg_path, "--lines", lines_path]
    old_argv = sys.argv
    buf = io.StringIO()
    try:
        sys.argv = argv
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
            bdr.main()                       # commit en cas de succès
    except SystemExit as exc:
        code = exc.code
        if code not in (0, None):
            raise ApplyError("Import BDR échoué : %s" % (buf.getvalue().strip()[-400:]
                                                          or code)) from exc
    except Exception as exc:  # noqa: BLE001
        raise ApplyError("Import BDR échoué : %s" % exc) from exc
    finally:
        sys.argv = old_argv


# --------------------------------------------------------------------------- #
def _apply_price(payload: dict, connect_kwargs: dict) -> None:
    import article_db  # type: ignore

    cfg = {
        "host": "localhost",
        "port": connect_kwargs.get("port", 3050),
        "database": connect_kwargs["database"],
        "user": connect_kwargs.get("user", "SYSDBA"),
        "password": connect_kwargs.get("password", ""),
        "charset": connect_kwargs.get("charset", "WIN1256"),
        "table": "ARTICLE",
    }
    changes = payload.get("changes") or []
    if not changes:
        raise ApplyError("Mise à jour de prix sans changements.")

    repo = article_db.ArticleRepository(cfg)
    try:
        repo.connect()
        repo.update_rows(changes)
        repo.commit()
    except article_db.DBError as exc:
        with contextlib.suppress(Exception):
            repo.rollback()
        raise ApplyError(str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        with contextlib.suppress(Exception):
            repo.rollback()
        raise ApplyError("Mise à jour de prix échouée : %s" % exc) from exc
    finally:
        repo.close()
