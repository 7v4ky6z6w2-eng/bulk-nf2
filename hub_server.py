"""Serveur hub — à lancer sur le poste du magasin 1 (toujours allumé).

Usage :
  python hub_server.py [--db central.db] [--port 5000] [--host 0.0.0.0]

Le serveur expose :
  * /api/*           — endpoints agents (sync push/pull, pending_ops)
  * /                — tableau de bord web Bootstrap 5 (lecture seule)
"""

from __future__ import annotations

import argparse
import logging
import logging.handlers
import os
import sys

from flask import Flask

# ─── path setup ────────────────────────────────────────────────────────────────
_SRC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from stores import StoreRegistry, StoresError, app_dir  # noqa: E402
from hub.central_db import init_db, connect            # noqa: E402
from hub.api import bp as api_bp                       # noqa: E402
from hub.dashboard import bp as dash_bp                # noqa: E402
from hub.mobile import bp as mobile_bp                 # noqa: E402
from notify import Notifier                            # noqa: E402

log = logging.getLogger("hub")


def create_app(db_path: str, api_key: str = "",
               notifier: Notifier | None = None,
               store_names: dict | None = None,
               registry=None) -> Flask:
    import hashlib
    from datetime import timedelta

    app = Flask(__name__, template_folder="templates")
    app.config["API_KEY"] = api_key
    app.config["ACCESS_CODE"] = getattr(registry, "access_code", "") if registry else ""
    app.config["store_names"] = store_names or {}
    app.config["notifier"] = notifier
    app.config["registry"] = registry

    # Clé de session (pages mobiles) : dérivée de la clé API / du code d'accès,
    # stable entre les redémarrages (les sessions du téléphone restent valides).
    seed = (api_key or app.config["ACCESS_CODE"] or "primenf-hub-defaut")
    app.secret_key = hashlib.sha256(seed.encode("utf-8")).digest()
    app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(days=30)
    app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
    app.config["MAX_CONTENT_LENGTH"] = 25 * 1024 * 1024  # 25 Mo (upload BDR)

    # Une connexion SQLite par thread (thread_local via closure)
    import threading
    _local = threading.local()

    def get_con():
        if not getattr(_local, "con", None):
            _local.con = connect(db_path)
        return _local.con

    app.config["db_connection"] = get_con

    # Thread de fond : vérifie les notifications manquées toutes les 2 min
    if notifier and notifier.enabled:
        def _notify_loop():
            import time
            while True:
                try:
                    con = connect(db_path)
                    notifier.notify_pending(con=con)
                    con.close()
                except Exception:  # noqa: BLE001
                    pass
                time.sleep(120)
        t = threading.Thread(target=_notify_loop, daemon=True, name="notifier")
        t.start()

    # Thread de fond : digest quotidien des ruptures de stock (une seule fois
    # par jour — digest_log empêche un doublon si le hub redémarre). Vérifie
    # toutes les 30 min, envoie seulement si le digest du jour n'est pas déjà parti.
    if notifier and notifier.enabled:
        def _low_stock_digest_loop():
            import time
            from datetime import datetime as _dt
            from hub.central_db import low_stock, digest_already_sent, mark_digest_sent
            while True:
                try:
                    con = connect(db_path)
                    today = _dt.now().strftime("%Y-%m-%d")
                    if not digest_already_sent(con, "low_stock", today):
                        rows = low_stock(con, threshold=3, limit=500)
                        if rows:
                            names = store_names or {}
                            lines = ["⚠️ Stock bas (%d ligne(s), seuil ≤3) :" % len(rows)]
                            for r in rows[:15]:
                                sname = names.get(r["store_id"], "Magasin %s" % r["store_id"])
                                lines.append("- %s : %s (%s)" % (
                                    sname, r.get("designation") or r["ref_art"], r["qte_stock"]))
                            if len(rows) > 15:
                                lines.append("… et %d autre(s)." % (len(rows) - 15))
                            notifier.send("\n".join(lines))
                        mark_digest_sent(con, "low_stock", today)
                    con.close()
                except Exception:  # noqa: BLE001
                    pass
                time.sleep(1800)
        threading.Thread(target=_low_stock_digest_loop, daemon=True,
                         name="low-stock-digest").start()

    # Thread de fond : purge les aperçus BDR mobiles abandonnés (jamais
    # confirmés ni annulés) — sinon ils s'accumulent indéfiniment dans le
    # dossier temporaire du système.
    def _sweep_loop():
        import time
        from hub.mobile import sweep_expired_previews
        while True:
            try:
                sweep_expired_previews()
            except Exception:  # noqa: BLE001
                pass
            time.sleep(600)
    threading.Thread(target=_sweep_loop, daemon=True, name="preview-sweep").start()

    # Filtre Jinja de formatage monétaire (partagé avec l'app bureau).
    from desktop.format import fmt_da, fmt_qty
    app.jinja_env.filters["fmt_da"] = fmt_da
    app.jinja_env.filters["fmt_qty"] = fmt_qty

    app.register_blueprint(api_bp)
    app.register_blueprint(dash_bp)
    app.register_blueprint(mobile_bp)
    return app


def main() -> None:
    parser = argparse.ArgumentParser(description="PrimeNF Hub server")
    parser.add_argument("--db", default="central.db", help="Chemin central.db")
    parser.add_argument("--port", type=int, default=5000)
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--stores-path", default=None)
    parser.add_argument("--log-level", default="INFO",
                        choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    args = parser.parse_args()

    # Toujours écrire un fichier de log (logs/hub.log) : lancé via pythonw
    # (tâche planifiée sans fenêtre) ou comme service NSSM, il n'y a AUCUNE
    # console pour lire quoi que ce soit — sys.stderr est carrément None, pas
    # juste invisible. Le StreamHandler ne casse rien dans ce cas (logging
    # avale silencieusement l'erreur d'écriture), mais sans fichier on perd
    # tout diagnostic. Toujours actif aussi en lancement console (start_hub.bat) :
    # on y voit ET la console ET le fichier.
    _log_dir = os.path.join(app_dir(), "logs")
    os.makedirs(_log_dir, exist_ok=True)
    _handlers = [logging.handlers.RotatingFileHandler(
        os.path.join(_log_dir, "hub.log"), maxBytes=5_000_000, backupCount=3,
        encoding="utf-8")]
    if sys.stderr is not None:
        _handlers.append(logging.StreamHandler())
    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=_handlers,
    )

    try:
        registry = StoreRegistry.load(args.stores_path) if args.stores_path \
            else StoreRegistry.load()
    except StoresError as exc:
        log.error("Configuration : %s", exc)
        sys.exit(1)

    log.info("Initialisation de la base centrale : %s", args.db)
    init_db(args.db)

    notifier = Notifier(
        bot_token=registry.notify.telegram_bot_token,
        chat_id=registry.notify.telegram_chat_id,
        ntfy_topic=registry.notify.ntfy_topic,
        store_names=registry.names(),
    )

    app = create_app(
        db_path=args.db,
        api_key=registry.hub_api_key,
        notifier=notifier,
        store_names=registry.names(),
        registry=registry,
    )

    log.info("Hub démarré sur %s:%d", args.host, args.port)
    app.run(host=args.host, port=args.port, threaded=True)


if __name__ == "__main__":
    main()
