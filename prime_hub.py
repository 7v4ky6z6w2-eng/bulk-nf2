"""Point d'entrée de l'application bureau PrimeNF Hub.

L'application est un client du hub (aucune base locale). Elle peut tourner sur
n'importe quel appareil pour consulter les tableaux de bord, importer des BDR et
éditer les prix. Deux façons de se configurer :

  1. stores.json présent (postes gérés : hub, magasins, PC de l'admin) → tout est
     déduit automatiquement, aucune saisie.
  2. Aucun stores.json (PC personnel, appareil quelconque) → un écran de
     connexion demande l'URL du hub + un code d'accès, puis l'app récupère la
     liste des magasins depuis le hub. Le code est mémorisé pour les fois
     suivantes. Aucun mot de passe Firebird n'est stocké sur l'appareil.

Usage :
  python prime_hub.py [--stores-path stores.json] [--hub URL] [--logout]
"""

from __future__ import annotations

import argparse
import os
import sys

_SRC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from PySide6.QtWidgets import QApplication, QMessageBox

from stores import StoreRegistry, StoresError
from desktop.hub_data import HubData
from desktop.main_window import MainWindow
from desktop import client_config
from desktop.login_dialog import LoginDialog


def _from_stores_json(path):
    """(registry, hub_url, code, data) depuis stores.json, ou None si absent."""
    try:
        registry = StoreRegistry.load(path) if path else StoreRegistry.load()
    except StoresError:
        return None
    hub_url = registry.hub_url()
    code = registry.access_code or registry.hub_api_key
    return registry, hub_url, code, HubData(hub_url, code)


def _from_login(saved):
    """Affiche le dialogue de connexion (pré-rempli) ; renvoie le tuple ou None."""
    dlg = LoginDialog(hub_url=saved.get("hub_url", ""), code=saved.get("code", ""))
    if dlg.exec() != LoginDialog.Accepted:
        return None
    client_config.save(dlg.hub_url, dlg.code)
    registry = StoreRegistry.from_client(dlg.stores, dlg.code)
    return registry, dlg.hub_url, dlg.code, dlg.data


def main() -> None:
    parser = argparse.ArgumentParser(description="PrimeNF Hub — Application bureau")
    parser.add_argument("--stores-path", default=None)
    parser.add_argument("--hub", default=None, help="Forcer l'URL du hub")
    parser.add_argument("--logout", action="store_true",
                        help="Oublier le hub/code mémorisés et redemander la connexion")
    args = parser.parse_args()

    app = QApplication(sys.argv)
    app.setApplicationName("PrimeNF Hub")
    app.setStyle("Fusion")

    if args.logout:
        client_config.clear()

    ctx = None

    # 1) stores.json (postes gérés) — sauf si on force une autre config
    if not args.logout:
        ctx = _from_stores_json(args.stores_path)

    # 2) code d'accès mémorisé (appareil déjà connecté une fois)
    if ctx is None and not args.logout:
        saved = client_config.load()
        if saved.get("hub_url") and saved.get("code") is not None:
            data = HubData(saved["hub_url"], saved["code"])
            try:
                cfg = data._get("/api/client/config")
                registry = StoreRegistry.from_client(cfg.get("stores", []), saved["code"])
                ctx = (registry, saved["hub_url"], saved["code"], data)
            except Exception:  # noqa: BLE001
                ctx = None

    # 3) écran de connexion (URL du hub + code)
    if ctx is None:
        ctx = _from_login(client_config.load())
    if ctx is None:
        return  # l'utilisateur a annulé

    registry, hub_url, code, data = ctx
    if args.hub:                       # surcharge éventuelle
        hub_url = args.hub
        data = HubData(hub_url, code)

    if not data.ping():
        QMessageBox.warning(
            None, "Hub injoignable",
            "Le hub n'est pas joignable à l'adresse :\n%s\n\n"
            "Vérifiez que le magasin 1 est allumé, que hub_server.py tourne et "
            "que Tailscale est connecté." % hub_url)

    win = MainWindow(registry, data)
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
