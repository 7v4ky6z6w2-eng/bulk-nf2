"""Pages mobiles : upload direct d'un Bon de Réception (Excel/PDF), édition de
prix, consultation du stock par magasin, correspondances par nom et
synchronisation de prix entre magasins — sans passer par l'application bureau.

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

from hub.central_db import (
    article_search, refs_for_match_key, stock_search, name_match_suggestions,
    confirm_article_link, price_sync_candidates, ArticleLinkConflict,
)
from hub.ops import submit_op
from hub.write_back import is_reachable, bdr_tiers, bdr_reconcile

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
    # Anti-redirection ouverte : seuls les chemins internes ("/…") sont suivis.
    nxt = request.args.get("next") or ""
    if not nxt.startswith("/") or nxt.startswith("//"):
        nxt = url_for("mobile.home")
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


_MISSING_RECON = {"ref_exists": None, "match_ref": None, "match_designation": None,
                  "match_score": None, "match_prix_achat": None, "status": "unknown"}


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

    # Rapprochement par désignation + liste des fournisseurs : nécessite une
    # connexion LIVE au magasin cible (Tailscale), donc seulement s'il est en
    # ligne. Hors ligne (ou en cas d'erreur), on dégrade proprement : toutes
    # les lignes passent en statut 'unknown', le sélecteur fournisseur devient
    # une simple saisie manuelle (cf. bdr_preview.html) — l'import reste
    # possible, juste sans aide au rapprochement pour cette fois.
    store = reg.get(store_id) if reg else None
    online = bool(store and is_reachable(store.host, store.port))
    tiers = None
    if online:
        try:
            kw = store.connect_kwargs()
            lines = bdr_reconcile(kw, lines)
            tiers = bdr_tiers(kw)
        except Exception:  # noqa: BLE001
            online = False
            lines = [dict(l, **_MISSING_RECON) for l in lines]
    else:
        lines = [dict(l, **_MISSING_RECON) for l in lines]

    token = _save_preview({"store_id": store_id, "config": cfg, "lines": lines,
                           "filename": file.filename})
    store_name = reg.get(store_id).name if reg else str(store_id)
    bad = sum(1 for l in lines if l.get("recon") is False)
    return render_template("mobile/bdr_preview.html", token=token, lines=lines,
                           store_name=store_name, filename=file.filename, bad_count=bad,
                           tiers=tiers, online=online)


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

    cfg = dict(data["config"])
    code_tiers = (request.form.get("code_tiers_select") or "").strip() \
        or (request.form.get("code_tiers_manual") or "").strip()
    cfg["code_tiers"] = code_tiers
    cfg["raison_sociale"] = (request.form.get("raison_sociale") or "").strip()

    final_lines = []
    for i, orig in enumerate(data["lines"]):
        if request.form.get("keep_%d" % i) != "on":
            continue
        line = {k: orig[k] for k in
               ("ref_art", "designation", "qte", "prix", "tva", "famille", "code_barres")}
        for form_key, line_key in (("qte_%d" % i, "qte"), ("prix_%d" % i, "prix")):
            raw = request.form.get(form_key)
            if raw not in (None, ""):
                try:
                    line[line_key] = float(raw.replace(",", "."))
                except ValueError:
                    pass
        pv_raw = request.form.get("prix_vente_%d" % i)
        if pv_raw not in (None, ""):
            try:
                line["prix_vente"] = float(pv_raw.replace(",", "."))
            except ValueError:
                pass
        if orig.get("status") == "matched" and request.form.get("lier_%d" % i) == "on":
            line["ref_art"] = orig["match_ref"]
        if request.form.get("maj_prix_achat_%d" % i) == "on":
            line["maj_prix_achat"] = True
        final_lines.append(line)

    if not final_lines:
        return render_template("mobile/bdr_result.html", ok=False,
                               message="Aucune ligne à importer (toutes décochées).")

    result = submit_op(_db(), _registry(), data["store_id"], "bdr_import",
                       {"config": cfg, "lines": final_lines},
                       op_uid="mobile-" + secure_filename(token))
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
        # Regroupement par match_key (code-barres partagé, sinon référence) :
        # le même produit vendu sous des références différentes selon le
        # magasin apparaît comme UNE ligne multi-magasins.
        by_key: dict = {}
        for r in rows:
            key = r.get("match_key") or r["ref_art"]
            g = by_key.setdefault(key, {"ref": r["ref_art"], "match_key": key,
                                        "designation": r.get("designation"),
                                        "prices": {}})
            g["prices"][r["store_id"]] = r.get("prixventeht")
        results = list(by_key.values())
    return render_template("mobile/prix_search.html", q=q, results=results,
                           store_names=_store_names())


@bp.get("/prix/edit")
def prix_edit_form():
    ref = request.args.get("ref", "")
    match_key = request.args.get("match_key", "")
    reg = _registry()
    return render_template("mobile/prix_edit.html", ref=ref, match_key=match_key,
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
                               match_key=(request.form.get("match_key") or ""),
                               stores=reg.stores if reg else [],
                               error="Article, prix (> 0) et au moins un magasin requis.")

    # Le même produit peut porter une référence différente par magasin : on
    # résout la référence PROPRE à chaque magasin via la clé de regroupement
    # (code-barres partagé), avec la référence affichée en repli.
    refs = refs_for_match_key(_db(), (request.form.get("match_key") or "").strip() or ref)
    results = []
    names = _store_names()
    for sid in store_ids:
        ref_sid = refs.get(sid, ref)
        changes = [{"ref0": ref_sid,
                    "values": {"PRIXVENTEHT": new_price, "PRIXVENTETTC": new_price}}]
        res = submit_op(_db(), reg, sid, "price_update", {"changes": changes})
        res["store_id"] = sid
        res["store_name"] = names.get(sid, "Magasin %s" % sid)
        results.append(res)

    return render_template("mobile/prix_result.html", ref=ref, price=new_price,
                           results=results)


# ── Stock par magasin (jamais un total combiné) ───────────────────────────────
@bp.get("/stock")
def stock_view():
    q = request.args.get("q", "").strip()
    reg = _registry()
    store_ids = [s.id for s in reg.stores] if reg else []
    results = []
    if q:
        rows = stock_search(_db(), q, limit=200)
        by_key: dict = {}
        for r in rows:
            key = r.get("match_key") or r["ref_art"]
            g = by_key.setdefault(key, {"ref": r["ref_art"], "designation": r.get("designation"),
                                        "qty": {}})
            g["qty"][r["store_id"]] = r.get("qte_stock")
        results = list(by_key.values())
    return render_template("mobile/stock.html", q=q, results=results,
                           store_ids=store_ids, store_names=_store_names())


# ── Suggestions de correspondance par nom ─────────────────────────────────────
@bp.get("/correspondances")
def correspondances():
    groups = name_match_suggestions(_db())
    return render_template("mobile/correspondances.html", groups=groups,
                           store_names=_store_names(), error=None)


@bp.post("/correspondances/confirmer")
def correspondances_confirm():
    members = [{"store_id": int(sid), "ref_art": ref}
              for sid, ref in zip(request.form.getlist("store_id"),
                                  request.form.getlist("ref_art"))
              if sid and ref]
    error = None
    try:
        confirm_article_link(_db(), members)
    except ArticleLinkConflict as exc:
        error = str(exc)
    groups = name_match_suggestions(_db())
    return render_template("mobile/correspondances.html", groups=groups,
                           store_names=_store_names(), error=error)


# ── Synchroniser les prix depuis un magasin ───────────────────────────────────
def _price_sync_rows(con, source_store_id: int) -> list:
    rows = []
    for g in price_sync_candidates(con, source_store_id):
        for t in g["targets"]:
            rows.append({"designation": g["designation"], "source_price": g["source_price"],
                         "store_id": t["store_id"], "ref_art": t["ref_art"],
                         "current_price": t["current_price"]})
    return rows


@bp.get("/prix/sync")
def prix_sync():
    reg = _registry()
    stores = reg.stores if reg else []
    source_arg = request.args.get("source")
    source = int(source_arg) if source_arg else (stores[0].id if stores else None)
    rows = _price_sync_rows(_db(), source) if source else []
    return render_template("mobile/prix_sync.html", stores=stores, source=source,
                           rows=rows, store_names=_store_names(), results=None)


@bp.post("/prix/sync/appliquer")
def prix_sync_apply():
    source = int(request.form.get("source") or 0)
    reg = _registry()
    names = _store_names()
    con = _db()
    results = []
    for idx in request.form.getlist("rows"):
        sid_s = request.form.get("store_id_%s" % idx)
        ref = request.form.get("ref_art_%s" % idx)
        price_s = request.form.get("price_%s" % idx)
        if not sid_s or not ref or not price_s:
            continue
        sid, price = int(sid_s), float(price_s)
        changes = [{"ref0": ref, "values": {"PRIXVENTEHT": price, "PRIXVENTETTC": price}}]
        res = submit_op(con, reg, sid, "price_update", {"changes": changes})
        res.update(store_id=sid, store_name=names.get(sid, "Magasin %s" % sid), ref_art=ref)
        results.append(res)

    stores = reg.stores if reg else []
    rows = _price_sync_rows(con, source) if source else []
    return render_template("mobile/prix_sync.html", stores=stores, source=source,
                           rows=rows, store_names=names, results=results)
