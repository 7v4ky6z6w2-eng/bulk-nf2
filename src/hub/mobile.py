"""Pages mobiles : upload direct d'un Bon de Réception (Excel/PDF) et édition de
prix depuis le téléphone, sans passer par l'application bureau.

Le téléphone envoie le FICHIER au hub (formulaire HTML classique) ; c'est le hub
qui le lit (openpyxl / pikepdf+pdfplumber, déjà nécessaires côté serveur) et
affiche un aperçu avant confirmation. L'application bureau JSON (/api/*) utilise
un header X-Api-Key ; ici, comme un navigateur de formulaire ne l'envoie pas
facilement, l'authentification se fait par SESSION (cookie signé) après une
page de connexion avec le code d'accès.
"""

from __future__ import annotations

import contextlib
import json
import os
import sys
import tempfile
import time
import uuid

from flask import (
    Blueprint, current_app, redirect, render_template, request,
    session, url_for,
)
from werkzeug.utils import secure_filename

from hub.central_db import article_search
from hub.ops import submit_op

_VENDOR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "vendor")
_BDR_DIR = os.path.join(_VENDOR, "bdr")
if _BDR_DIR not in sys.path:
    sys.path.insert(0, _BDR_DIR)

bp = Blueprint("mobile", __name__, url_prefix="/m")

_PREVIEW_TTL = 30 * 60  # 30 minutes
_ALLOWED_EXT = {".xlsx", ".xlsm", ".pdf"}


def _db():
    return current_app.config["db_connection"]()


def _registry():
    return current_app.config.get("registry")


def _store_names() -> dict:
    return current_app.config.get("store_names", {})


# ── Authentification par session ──────────────────────────────────────────────
@bp.before_request
def _require_login():
    if request.endpoint in ("mobile.login_form", "mobile.login_submit"):
        return None
    if not session.get("mobile_auth"):
        return redirect(url_for("mobile.login_form", next=request.path))
    return None


@bp.get("/login")
def login_form():
    return render_template("mobile/login.html", error=None)


@bp.post("/login")
def login_submit():
    code = (request.form.get("code") or "").strip()
    valid = {v for v in (current_app.config.get("API_KEY", ""),
                         current_app.config.get("ACCESS_CODE", "")) if v}
    if valid and code not in valid:
        return render_template("mobile/login.html", error="Code invalide.")
    session["mobile_auth"] = True
    session.permanent = True
    nxt = request.args.get("next") or url_for("mobile.home")
    return redirect(nxt)


@bp.get("/logout")
def logout():
    session.clear()
    return redirect(url_for("mobile.login_form"))


# ── Accueil ────────────────────────────────────────────────────────────────────
@bp.get("/")
def home():
    reg = _registry()
    stores = reg.stores if reg else []
    return render_template("mobile/home.html", stores=stores)


# ── Aperçus temporaires (upload BDR) ──────────────────────────────────────────
def _preview_dir() -> str:
    d = os.path.join(tempfile.gettempdir(), "primenf_mobile_previews")
    os.makedirs(d, exist_ok=True)
    return d


def _save_preview(data: dict) -> str:
    token = uuid.uuid4().hex
    path = os.path.join(_preview_dir(), token + ".json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False)
    return token


def _load_preview(token: str) -> dict | None:
    """Lecture SANS consommer (aperçu, annulation) : ne supprime que si expiré."""
    token = secure_filename(token or "")
    path = os.path.join(_preview_dir(), token + ".json")
    if not os.path.isfile(path):
        return None
    if time.time() - os.path.getmtime(path) > _PREVIEW_TTL:
        os.remove(path)
        return None
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def _claim_preview(token: str) -> dict | None:
    """Récupère ET consomme un aperçu de façon ATOMIQUE (os.rename).

    Empêche un double-tap / double-soumission (réseau mobile lent, retry
    client) d'appliquer deux fois le même BDR : seule la requête qui gagne le
    rename obtient les données ; la seconde reçoit None (déjà traité).
    """
    token = secure_filename(token or "")
    src = os.path.join(_preview_dir(), token + ".json")
    dst = os.path.join(_preview_dir(), token + ".claimed.json")
    try:
        os.rename(src, dst)
    except OSError:
        return None  # déjà réclamé par une autre requête, ou inexistant/expiré
    try:
        if time.time() - os.path.getmtime(dst) > _PREVIEW_TTL:
            return None
        with open(dst, "r", encoding="utf-8") as fh:
            return json.load(fh)
    finally:
        with contextlib.suppress(OSError):
            os.remove(dst)


def _delete_preview(token: str) -> None:
    token = secure_filename(token or "")
    path = os.path.join(_preview_dir(), token + ".json")
    if os.path.isfile(path):
        os.remove(path)


def sweep_expired_previews() -> None:
    """Nettoie les aperçus abandonnés (jamais confirmés ni annulés)."""
    d = _preview_dir()
    try:
        names = os.listdir(d)
    except OSError:
        return
    now = time.time()
    for name in names:
        path = os.path.join(d, name)
        try:
            if now - os.path.getmtime(path) > _PREVIEW_TTL:
                os.remove(path)
        except OSError:
            continue


# ── Import BDR depuis le téléphone ────────────────────────────────────────────
@bp.get("/bdr")
def bdr_upload_form():
    reg = _registry()
    return render_template("mobile/bdr_upload.html", stores=reg.stores if reg else [],
                           error=None)


@bp.post("/bdr/upload")
def bdr_upload():
    reg = _registry()
    store_id = int(request.form.get("store_id") or 0)
    file = request.files.get("file")
    if not store_id or not file or not file.filename:
        return render_template("mobile/bdr_upload.html", stores=reg.stores if reg else [],
                               error="Choisissez un magasin et un fichier.")
    ext = os.path.splitext(file.filename)[1].lower()
    if ext not in _ALLOWED_EXT:
        return render_template("mobile/bdr_upload.html", stores=reg.stores if reg else [],
                               error="Format non supporté (Excel .xlsx/.xlsm ou PDF uniquement).")

    tmp_path = os.path.join(tempfile.gettempdir(),
                            "bdr_upload_%s%s" % (uuid.uuid4().hex, ext))
    file.save(tmp_path)
    try:
        import import_bon_reception as bdr  # type: ignore
        cfg = bdr.load_config(None)
        lines = bdr.read_pdf(tmp_path, cfg) if ext == ".pdf" else bdr.read_excel(tmp_path, cfg)
    except Exception as exc:  # noqa: BLE001
        return render_template("mobile/bdr_upload.html", stores=reg.stores if reg else [],
                               error="Erreur de lecture : %s" % exc)
    finally:
        try:
            os.remove(tmp_path)
        except OSError:
            pass

    if not lines:
        return render_template("mobile/bdr_upload.html", stores=reg.stores if reg else [],
                               error="Aucune ligne trouvée dans le fichier.")

    token = _save_preview({"store_id": store_id, "config": cfg, "lines": lines,
                           "filename": file.filename})
    store_name = reg.get(store_id).name if reg else str(store_id)
    bad = sum(1 for l in lines if l.get("recon") is False)
    return render_template("mobile/bdr_preview.html", token=token, lines=lines,
                           store_name=store_name, filename=file.filename, bad_count=bad)


@bp.post("/bdr/confirm")
def bdr_confirm():
    token = request.form.get("token", "")
    # Réclamation ATOMIQUE : un double-tap / retry réseau ne peut appliquer le
    # BDR qu'une seule fois (la seconde requête reçoit None, pas les données).
    data = _claim_preview(token)
    if not data:
        return render_template("mobile/bdr_result.html", ok=False,
                               message="Aperçu expiré ou déjà confirmé (recommencez l'envoi "
                                       "si l'import n'a pas eu lieu).")
    result = submit_op(_db(), _registry(), data["store_id"], "bdr_import",
                       {"config": data["config"], "lines": data["lines"]})
    names = _store_names()
    store_name = names.get(data["store_id"], "Magasin %s" % data["store_id"])
    return render_template("mobile/bdr_result.html", ok=(result["status"] != "error"),
                           result=result, store_name=store_name)


@bp.post("/bdr/cancel")
def bdr_cancel():
    _delete_preview(request.form.get("token", ""))
    return redirect(url_for("mobile.bdr_upload_form"))


# ── Édition de prix depuis le téléphone ───────────────────────────────────────
@bp.get("/prix")
def prix_search():
    q = request.args.get("q", "").strip()
    results = []
    if q:
        rows = article_search(_db(), q)
        by_ref: dict = {}
        for r in rows:
            ref = r["ref_art"]
            by_ref.setdefault(ref, {"ref": ref, "designation": r.get("designation"),
                                    "prices": {}})
            by_ref[ref]["prices"][r["store_id"]] = r.get("prixventeht")
        results = list(by_ref.values())
    return render_template("mobile/prix_search.html", q=q, results=results,
                           store_names=_store_names())


@bp.get("/prix/edit")
def prix_edit_form():
    ref = request.args.get("ref", "")
    reg = _registry()
    return render_template("mobile/prix_edit.html", ref=ref,
                           stores=reg.stores if reg else [])


@bp.post("/prix/apply")
def prix_apply():
    ref = (request.form.get("ref") or "").strip()
    try:
        new_price = float((request.form.get("price") or "0").replace(",", "."))
    except ValueError:
        new_price = 0
    store_ids = [int(v) for v in request.form.getlist("store_id")]
    reg = _registry()

    if not ref or new_price <= 0 or not store_ids:
        return render_template("mobile/prix_edit.html", ref=ref,
                               stores=reg.stores if reg else [],
                               error="Article, prix (> 0) et au moins un magasin requis.")

    changes = [{"ref0": ref, "values": {"PRIXVENTEHT": new_price, "PRIXVENTETTC": new_price}}]
    results = []
    names = _store_names()
    for sid in store_ids:
        res = submit_op(_db(), reg, sid, "price_update", {"changes": changes})
        res["store_id"] = sid
        res["store_name"] = names.get(sid, "Magasin %s" % sid)
        results.append(res)

    return render_template("mobile/prix_result.html", ref=ref, price=new_price,
                           results=results)
