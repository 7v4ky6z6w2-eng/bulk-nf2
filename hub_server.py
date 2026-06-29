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
import os
import sys

from flask import Flask

# ─── path setup ────────────────────────────────────────────────────────────────
_SRC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from stores import StoreRegistry, StoresError          # noqa: E402
from hub.central_db import init_db, connect            # noqa: E402
from hub.api import bp as api_bp                       # noqa: E402
from hub.dashboard import bp as dash_bp                # noqa: E402
from notify import Notifier                            # noqa: E402

log = logging.getLogger("hub")


def create_app(db_path: str, api_key: str = "",
               notifier: Notifier | None = None,
               store_names: dict | None = None) -> Flask:
    app = Flask(__name__, template_folder="templates")
    app.config["API_KEY"] = api_key
    app.config["store_names"] = store_names or {}
    app.config["notifier"] = notifier

    # Une connexion SQLite par thread (thread_local via closure)
    import threading
    _local = threading.local()

    def get_con():
        if not getattr(_local, "con", None):
            _local.con = connect(db_path)
        return _local.con

    app.config["db_connection"] = get_con

    app.register_blueprint(api_bp)
    app.register_blueprint(dash_bp)
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

    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
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
    )

    log.info("Hub démarré sur %s:%d", args.host, args.port)
    app.run(host=args.host, port=args.port, threaded=True)


if __name__ == "__main__":
    main()
