"""Point d'entrée de l'application Affichage Prix Netfact.

Utilisation :
    python src/main.py                  # lancement normal (interface kiosque)
    python src/main.py --selftest       # test de connexion sans Qt
    python src/main.py --selftest --code 1234567890  # idem avec un code article

Le dossier src/ est ajouté au sys.path afin que les modules config, i18n, etc.
soient importables directement (convention du projet).
"""

from __future__ import annotations

import argparse
import os
import sys

# --- Ajout de src/ au chemin de recherche des modules ---------------------
# Fonctionne aussi bien en exécution directe (python src/main.py) qu'une fois
# packagé avec PyInstaller (sys.executable est dans le dossier de l'exe).
_SRC_DIR = os.path.dirname(os.path.abspath(__file__))
if _SRC_DIR not in sys.path:
    sys.path.insert(0, _SRC_DIR)

import config as _config_module


# ---------------------------------------------------------------------------
# Mode test autonome (--selftest) — sans Qt
# ---------------------------------------------------------------------------

def _selftest(code: str | None) -> int:
    """Teste la connexion DB et éventuellement WooCommerce sans Qt.

    Code de retour : 0 = succès DB, 1 = échec DB, 2 = configuration incomplète.
    """
    cfg = _config_module.load()

    if not cfg.is_complete():
        print("Config incomplete")
        return 2

    # --- Test base de données
    from database import Database, DatabaseError

    db = Database(cfg.firebird)
    db_ok = False
    try:
        db.test_connection()
        print("DB: PASS")
        db_ok = True
    except DatabaseError as exc:
        print(f"DB: FAIL — {exc}")
    except Exception as exc:
        print(f"DB: FAIL — {exc}")

    # Recherche optionnelle d'un article
    if db_ok and code:
        try:
            article = db.lookup_article(code)
            if article:
                print(
                    f"Article ({code}): {article.designation!r} | "
                    f"Prix HT : {article.prix_vente_ht:.2f} | "
                    f"REF_ART : {article.ref_art!r}"
                )
            else:
                print(f"Article ({code}): introuvable")
        except DatabaseError as exc:
            print(f"Article ({code}): erreur — {exc}")

    db.close()

    # --- Test WooCommerce (si configuré)
    from woocommerce import WooClient

    woo = WooClient(cfg.woocommerce)
    if woo.configured:
        try:
            woo.test()
            print("WOO: PASS")
        except Exception as exc:
            print(f"WOO: FAIL — {exc}")
    else:
        print("WOO: non configuré (ignoré)")

    return 0 if db_ok else 1


# ---------------------------------------------------------------------------
# Lancement normal (interface kiosque Qt)
# ---------------------------------------------------------------------------

def _launch_gui(args: argparse.Namespace) -> int:
    # Import Tkinter uniquement en mode GUI
    import tkinter as tk

    root = tk.Tk()
    root.withdraw()  # cachée le temps de la configuration éventuelle

    # Chargement de la configuration
    cfg = _config_module.load()

    # Si la configuration est incomplète, ouvrir l'assistant de configuration
    if not cfg.is_complete():
        from ui.setup_window import SetupDialog

        dlg = SetupDialog(root, cfg)
        if not dlg.result:
            # L'utilisateur a annulé : on quitte proprement
            root.destroy()
            return 0

        # Recharger après sauvegarde par l'assistant
        cfg = _config_module.load()

        if not cfg.is_complete():
            # Toujours incomplet après l'assistant (ne devrait pas arriver)
            root.destroy()
            return 1

    # --- Instanciation des services
    from database import Database
    from woocommerce import WooClient
    from ui.kiosk_window import KioskWindow

    db = Database(cfg.firebird)
    woo = WooClient(cfg.woocommerce)

    root.deiconify()
    KioskWindow(root, cfg, db, woo)
    root.mainloop()

    # Nettoyage
    db.close()

    return 0


# ---------------------------------------------------------------------------
# Point d'entrée
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Affichage Prix Netfact — kiosque code-barres"
    )
    parser.add_argument(
        "--selftest",
        action="store_true",
        help="Tester la connexion sans démarrer l'interface graphique",
    )
    parser.add_argument(
        "--code",
        metavar="CODE",
        default=None,
        help="Code article ou code-barres à rechercher lors du selftest",
    )
    args = parser.parse_args()

    if args.selftest:
        sys.exit(_selftest(args.code))
    else:
        sys.exit(_launch_gui(args))


if __name__ == "__main__":
    main()
