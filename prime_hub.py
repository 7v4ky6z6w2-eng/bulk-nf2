"""Point d'entrée de l'application bureau PrimeNF Hub.

Usage :
  python prime_hub.py [--db central.db] [--stores-path stores.json]
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
from desktop.main_window import MainWindow


def main() -> None:
    parser = argparse.ArgumentParser(description="PrimeNF Hub — Application bureau")
    parser.add_argument("--db", default="central.db", help="Chemin vers central.db")
    parser.add_argument("--stores-path", default=None)
    args = parser.parse_args()

    app = QApplication(sys.argv)
    app.setApplicationName("PrimeNF Hub")
    app.setStyle("Fusion")

    try:
        registry = StoreRegistry.load(args.stores_path) if args.stores_path \
            else StoreRegistry.load()
    except StoresError as exc:
        QMessageBox.critical(None, "Erreur de configuration", str(exc))
        sys.exit(1)

    win = MainWindow(registry, args.db)
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
