"""Assistant de première configuration / réglages (Tkinter).

Permet de saisir les informations de connexion au serveur Firebird et au site
WooCommerce, de les tester, puis de les enregistrer dans config.ini.

Réécrit en Tkinter (au lieu de Qt/PySide2) pour la compatibilité 32 bits.
"""

from __future__ import annotations

import tkinter as tk
from tkinter import messagebox, ttk

import i18n
from config import AppConfig, FirebirdConfig, WooConfig, save
from database import Database
from woocommerce import WooClient


class SetupDialog:
    """Boîte de dialogue modale de configuration.

    Après fermeture, l'attribut `result` vaut True si l'utilisateur a
    enregistré, False s'il a annulé / fermé la fenêtre.
    """

    def __init__(self, root: tk.Tk, cfg: AppConfig):
        self._cfg = cfg
        self.result = False

        self.win = tk.Toplevel(root)
        self.win.title(i18n.SETUP_TITLE)
        self.win.configure(padx=18, pady=18)
        self.win.resizable(False, False)

        self._build_ui()
        self._load_values()

        # S'assurer que la fenêtre est visible et au premier plan, même si le
        # parent est masqué (withdraw). Sans cela elle peut rester invisible.
        self.win.update_idletasks()
        self.win.deiconify()
        self.win.lift()
        self.win.attributes("-topmost", True)
        self.win.after(300, lambda: self.win.attributes("-topmost", False))
        self.win.focus_force()
        try:
            self.win.grab_set()
        except tk.TclError:
            pass

        self.win.protocol("WM_DELETE_WINDOW", self._on_cancel)

        root.wait_window(self.win)

    # --- Construction ------------------------------------------------------

    def _build_ui(self) -> None:
        intro = tk.Label(
            self.win,
            text=("Renseignez la connexion au serveur Netfact (base de données) "
                  "puis, si besoin, votre site WooCommerce pour les photos."),
            wraplength=460, justify="left",
        )
        intro.grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 12))

        # --- Base de données
        fb = ttk.LabelFrame(self.win, text="Serveur Netfact (base de données)",
                            padding=10)
        fb.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(0, 10))
        fb.columnconfigure(1, weight=1)

        self.host = self._row(fb, 0, i18n.SETUP_SERVER)
        self.port = self._row(fb, 1, i18n.SETUP_PORT)
        self.database = self._row(fb, 2, i18n.SETUP_DATABASE, width=42)
        self.user = self._row(fb, 3, i18n.SETUP_USER)
        self.password = self._row(fb, 4, i18n.SETUP_PASSWORD, show="*")

        # --- WooCommerce
        woo = ttk.LabelFrame(self.win, text="Site WooCommerce (photos — optionnel)",
                             padding=10)
        woo.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(0, 10))
        woo.columnconfigure(1, weight=1)

        self.woo_url = self._row(woo, 0, i18n.SETUP_WOO_URL, width=42)
        self.woo_key = self._row(woo, 1, i18n.SETUP_WOO_KEY)
        self.woo_secret = self._row(woo, 2, i18n.SETUP_WOO_SECRET, show="*")

        # --- Boutons
        btns = tk.Frame(self.win)
        btns.grid(row=3, column=0, columnspan=2, sticky="ew", pady=(6, 0))
        tk.Button(btns, text=i18n.SETUP_TEST, command=self._on_test).pack(side="left")
        tk.Button(btns, text=i18n.SETUP_SAVE, command=self._on_save,
                  default="active").pack(side="right")

    def _row(self, parent, r, label, width=24, show=None) -> tk.Entry:
        tk.Label(parent, text=label).grid(row=r, column=0, sticky="w",
                                          padx=(0, 10), pady=3)
        entry = tk.Entry(parent, width=width, show=show)
        entry.grid(row=r, column=1, sticky="ew", pady=3)
        return entry

    def _load_values(self) -> None:
        fb = self._cfg.firebird
        self.host.insert(0, fb.host)
        self.port.insert(0, str(fb.port or 3050))
        self.database.insert(0, fb.database)
        self.user.insert(0, fb.user or "SYSDBA")
        self.password.insert(0, fb.password)
        woo = self._cfg.woocommerce
        self.woo_url.insert(0, woo.base_url)
        self.woo_key.insert(0, woo.consumer_key)
        self.woo_secret.insert(0, woo.consumer_secret)

    # --- Collecte ----------------------------------------------------------

    def _collect(self) -> AppConfig:
        cfg = self._cfg
        try:
            port = int(self.port.get().strip() or "3050")
        except ValueError:
            port = 3050
        cfg.firebird = FirebirdConfig(
            host=self.host.get().strip(),
            port=port,
            database=self.database.get().strip(),
            user=self.user.get().strip() or "SYSDBA",
            password=self.password.get(),
        )
        cfg.woocommerce = WooConfig(
            base_url=self.woo_url.get().strip().rstrip("/"),
            consumer_key=self.woo_key.get().strip(),
            consumer_secret=self.woo_secret.get().strip(),
        )
        return cfg

    # --- Actions -----------------------------------------------------------

    def _on_test(self) -> None:
        cfg = self._collect()
        messages = []
        ok = True

        db = Database(cfg.firebird)
        try:
            db.test_connection()
            messages.append("OK  Base de données : " + i18n.SETUP_TEST_OK)
        except Exception as exc:  # noqa: BLE001
            ok = False
            messages.append(f"ECHEC  Base de données : {exc}")
        finally:
            db.close()

        woo = WooClient(cfg.woocommerce)
        if woo.configured:
            try:
                woo.test()
                messages.append("OK  WooCommerce : " + i18n.SETUP_TEST_OK)
            except Exception as exc:  # noqa: BLE001
                messages.append(f"ATTENTION  WooCommerce : {exc}")
        else:
            messages.append("WooCommerce non configuré (photos désactivées)")

        title = i18n.SETUP_TEST_OK if ok else i18n.SETUP_TEST_FAIL
        if ok:
            messagebox.showinfo(title, "\n".join(messages), parent=self.win)
        else:
            messagebox.showwarning(title, "\n".join(messages), parent=self.win)

    def _on_save(self) -> None:
        cfg = self._collect()
        if not cfg.is_complete():
            messagebox.showwarning(
                "Champs manquants",
                "Veuillez renseigner le chemin de la base, "
                "l'utilisateur et le mot de passe.",
                parent=self.win,
            )
            return
        save(cfg)
        self.result = True
        self.win.destroy()

    def _on_cancel(self) -> None:
        self.result = False
        self.win.destroy()
