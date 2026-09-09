#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
bulk_article_editor.py
======================

Editeur EN MASSE des articles de la base PRIME (Firebird), avec interface
graphique (Tkinter). Permet de modifier rapidement, pour plusieurs articles
a la fois :

    * le PRIX DE VENTE  (PRIXVENTEHT / PRIXVENTETTC)
    * la REFERENCE      (REF_ART)
    * les CODES EQUIVALENTS (table EQUIV_CBARRES : plusieurs codes-barres
      par article, separes par ';' dans la grille)

Fonctions principales
---------------------
  - Connexion a la base via config.json (meme format que votre import).
  - Recherche / filtre des articles (reference, code-barres, designation).
  - Edition d'une cellule par double-clic (valeur tracee "en attente").
  - Operations en masse sur la SELECTION :
        * Prix : fixer / +%, -% / +montant, -montant / arrondi (.99, .95, 0,50...)
                 au choix sur le HT ou le TTC, avec recalcul automatique de
                 l'autre via le taux de TVA.
        * Codes equivalents : ajouter / vider / ajouter la reference.
        * Reference : prefixe / suffixe / chercher-remplacer.
  - Recap des modifications en attente, puis ENREGISTRER (commit) ou ANNULER
    (rollback) — le tout dans une seule transaction.
  - Import d'un fichier Excel/CSV pour mettre a jour prix & code-barres par
    reference, et export de la vue courante en CSV.

Lancement
---------
    python bulk_article_editor.py                 # demande le config.json
    python bulk_article_editor.py --config config.json
    python bulk_article_editor.py --demo          # donnees de demo, sans base

Dependances : fdb (Firebird), openpyxl (Excel). Tkinter est livre avec Python.
"""

from __future__ import annotations

import argparse
import csv
import os
import sys

import tkinter as tk
from tkinter import ttk, filedialog, messagebox, simpledialog

from editor_logic import (Cols, PriceOp, TextOp, fmt_price, fmt_date, parse_number,
                          parse_bool, encoded_len, split_codes, join_codes)
import article_db
from article_db import ArticleRepository, DemoRepository, DBError, load_config, save_config
import label_print


APP_TITLE = "PRIME — Editeur en masse des articles"
ROUND_CHOICES = [("(aucun)", None), (".99", "0.99"), (".95", "0.95"),
                 (".90", "0.90"), ("0,50", "0.50"), ("0,10", "0.10"),
                 ("0,05", "0.05"), ("entier", "1")]
CHARSETS = ["WIN1252", "ISO8859_1", "ISO8859_15", "UTF8", "NONE", "DOS850"]


def friendly_error(raw):
    """Traduit les pannes Firebird courantes en francais lisible."""
    t = (raw or "").lower()
    rules = [
        (("no module named 'fdb'", "module 'fdb'", "import fdb"),
         "Le pilote Firebird « fdb » n'est pas installe (py -m pip install fdb)."),
        (("fbclient", "client library", "load_api", "libfbclient",
          "valid win32", "specified module", "dll load"),
         "Librairie cliente Firebird introuvable ou de mauvaise architecture "
         "(fbclient.dll 32/64 bits). Indiquez son chemin dans « fbclient »."),
        (("no such file", "cannot open", "unable to open", "i/o error",
          "error while trying to open file", "no permission",
          "unavailable database", "file is not a valid database"),
         "Base introuvable ou inaccessible : verifiez le chemin du fichier .FDB "
         "(ex : C:/oransoft/netfact2/data/primeoffice2026.fdb)."),
        (("user name", "login", "incorrect user", "install_check",
          "your user name and password", "password"),
         "Identifiant ou mot de passe incorrect (par defaut SYSDBA / masterkey)."),
        (("connection refused", "rejected", "network", "failed to establish",
          "unable to complete network"),
         "Serveur Firebird injoignable : verifiez l'hote / le port, ou laissez "
         "l'hote VIDE pour un acces local au fichier (embedded)."),
        (("malformed string", "transliteration", "charset", "character set"),
         "Probleme d'encodage : essayez un autre charset (WIN1252 ou NONE)."),
        (("unsupported on-disk structure", "ods"),
         "Version de base (ODS) non supportee par la librairie cliente "
         "installee : utilisez le fbclient.dll fourni avec votre logiciel PRIME."),
        (("table", "introuvable dans la base"),
         "Table des articles introuvable : verifiez le nom de la table "
         "(par defaut ARTICLE)."),
    ]
    for keys, msg in rules:
        if any(k in t for k in keys):
            return msg
    s = (raw or "").strip()
    return s.splitlines()[-1][:300] if s else "Erreur inconnue."


# --------------------------------------------------------------------------- #
#  Boite de dialogue de connexion
# --------------------------------------------------------------------------- #
class ConnectDialog(tk.Toplevel):
    """Ecran de connexion : choisir la base, tester la connexion, se connecter.

    A la fermeture :
      * self.result = dict de config si l'utilisateur s'est connecte (None sinon)
      * self.repo   = le repository deja connecte (pour eviter une reconnexion)
      * self.demo   = True si l'utilisateur a choisi le mode demonstration
    """

    def __init__(self, master, cfg, allow_demo=True):
        super().__init__(master)
        self.title("Connexion a la base PRIME (Firebird)")
        self.resizable(False, False)
        self.result = None
        self.repo = None
        self.demo = False
        self.cfg = dict(cfg)
        self.vars = {}

        frm = ttk.Frame(self, padding=14)
        frm.grid(row=0, column=0, sticky="nsew")
        ttk.Label(frm, text="Base de donnees PRIME (Firebird)",
                  font=("", 11, "bold")).grid(row=0, column=0, columnspan=3, sticky="w")
        ttk.Label(frm, foreground="#666",
                  text="Choisissez le fichier .FDB puis testez la connexion avant d'ouvrir."
                  ).grid(row=1, column=0, columnspan=3, sticky="w", pady=(0, 8))

        rows = [
            ("database", "Fichier .FDB", 46, "file"),
            ("host", "Hote (vide = acces local au fichier)", 30, None),
            ("port", "Port", 30, None),
            ("user", "Utilisateur", 30, None),
            ("password", "Mot de passe", 30, None),
            ("charset", "Charset", 30, "charset"),
            ("table", "Table des articles", 30, None),
            ("fb_client_library", "fbclient.dll (optionnel)", 46, "file"),
        ]
        r = 2
        for key, label, width, kind in rows:
            ttk.Label(frm, text=label).grid(row=r, column=0, sticky="w", pady=2, padx=(0, 8))
            var = tk.StringVar(value=str(self.cfg.get(key, "") or ""))
            self.vars[key] = var
            if kind == "charset":
                w = ttk.Combobox(frm, textvariable=var, values=CHARSETS, width=width - 3)
            else:
                show = "*" if key == "password" else ""
                w = ttk.Entry(frm, textvariable=var, width=width, show=show)
            w.grid(row=r, column=1, sticky="we", pady=2)
            if kind == "file":
                ttk.Button(frm, text="Parcourir…", width=11,
                           command=lambda v=var, k=key: self._browse(v, k)
                           ).grid(row=r, column=2, padx=4)
            r += 1

        # ligne test connexion
        test_row = ttk.Frame(frm)
        test_row.grid(row=r, column=0, columnspan=3, sticky="we", pady=(10, 2))
        ttk.Button(test_row, text="Tester la connexion", command=self._test).pack(side="left")
        self.conn_status = tk.StringVar(value="")
        self.status_lbl = tk.Label(test_row, textvariable=self.conn_status, anchor="w",
                                   justify="left", wraplength=420)
        self.status_lbl.pack(side="left", padx=10, fill="x", expand=True)
        r += 1

        # boutons
        btns = ttk.Frame(frm)
        btns.grid(row=r, column=0, columnspan=3, pady=(12, 0), sticky="e")
        if allow_demo:
            ttk.Button(btns, text="Mode demo", command=self._demo).pack(side="left", padx=4)
        ttk.Button(btns, text="Se connecter", command=self._ok).pack(side="left", padx=4)
        ttk.Button(btns, text="Annuler", command=self.destroy).pack(side="left")

        frm.columnconfigure(1, weight=1)
        self.bind("<Return>", lambda e: self._ok())
        self.bind("<Escape>", lambda e: self.destroy())
        self.transient(master)
        self.grab_set()
        self.update_idletasks()
        self._center(master)

    def _center(self, master):
        try:
            self.geometry("+%d+%d" % (master.winfo_rootx() + 60, master.winfo_rooty() + 60))
        except tk.TclError:
            pass

    def _browse(self, var, key):
        if key == "fb_client_library":
            path = filedialog.askopenfilename(
                parent=self, title="Choisir fbclient",
                filetypes=[("Librairies", "*.dll *.so *.dylib"), ("Tous", "*.*")])
        else:
            path = filedialog.askopenfilename(
                parent=self, title="Choisir le fichier de base Firebird",
                filetypes=[("Base Firebird", "*.fdb *.FDB *.gdb"), ("Tous", "*.*")])
        if path:
            var.set(path)

    def _collect(self):
        cfg = dict(self.cfg)
        for key, var in self.vars.items():
            cfg[key] = var.get().strip()
        return cfg

    def _set_status(self, state, msg):
        color = {True: "#1f7a33", False: "#a11", None: "#555"}[state]
        mark = {True: "✓ ", False: "✗ ", None: "" }[state]
        self.conn_status.set(mark + msg)
        self.status_lbl.configure(fg=color)
        self.update_idletasks()

    def _test(self):
        cfg = self._collect()
        if not cfg.get("database"):
            self._set_status(False, "Indiquez d'abord le fichier .FDB.")
            return
        self._set_status(None, "Connexion en cours…")
        repo = None
        try:
            repo = ArticleRepository(cfg).connect()
            n = repo.count()
            fam = len(repo.list_familles()) if repo.has_famille_ref() else 0
            self._set_status(True, "Connexion reussie : %d article(s), %d famille(s) "
                             "(table %s)." % (n, fam, repo.table))
        except DBError as exc:
            self._set_status(False, friendly_error(str(exc)))
        except Exception as exc:                       # noqa: BLE001
            self._set_status(False, friendly_error(str(exc)))
        finally:
            if repo is not None:
                repo.close()

    def _ok(self):
        cfg = self._collect()
        if not cfg.get("database"):
            self._set_status(False, "Indiquez le fichier .FDB.")
            return
        self._set_status(None, "Connexion en cours…")
        try:
            self.repo = ArticleRepository(cfg).connect()
        except DBError as exc:
            self._set_status(False, friendly_error(str(exc)))
            return
        except Exception as exc:                       # noqa: BLE001
            self._set_status(False, friendly_error(str(exc)))
            return
        self.result = cfg
        self.destroy()

    def _demo(self):
        self.demo = True
        self.result = None
        self.destroy()


# --------------------------------------------------------------------------- #
#  Application principale
# --------------------------------------------------------------------------- #
class BulkEditorApp(ttk.Frame):
    def __init__(self, master, repo, config_path=None):
        super().__init__(master, padding=6)
        self.master = master
        self.repo = repo
        self.config_path = config_path
        self.pack(fill="both", expand=True)

        self.rows = []            # liste de dicts (donnees affichees)
        self.row_by_iid = {}      # iid Treeview -> dict
        self.pending = {}         # ref0 -> {colonne_logique: nouvelle_valeur}
        self.new_familles = {}    # code -> (intitule, tva) familles a creer au commit
        self.pending_tarifs = {}  # ref0 -> {type_code: prix}
        self.tarif_data = {}      # ref0 -> {type_code: prix} charge depuis la base
        self.pending_equiv = {}   # ref0 -> [codes] (remplace la liste complete)
        self.equiv_data = {}      # ref0 -> [codes] charges depuis la base
        self._sort_col = None     # colonne de tri active (None = ordre naturel)
        self._sort_rev = False    # True = descendant

        self.columns = repo.display_columns()
        self.equiv_enabled = getattr(repo, "has_equiv", lambda: False)()
        if self.equiv_enabled:
            pos = (self.columns.index(Cols.DESIGNATION) + 1
                   if Cols.DESIGNATION in self.columns else 1)
            self.columns.insert(pos, Cols.CODES_EQUIV)
        try:
            self.tarif_types = repo.load_tarif_types()
        except Exception:         # noqa: BLE001
            self.tarif_types = []
        self.tarif_labels = {"__tarif_%s__" % c: n for c, n in self.tarif_types}
        for code, _ in self.tarif_types:
            self.columns.append("__tarif_%s__" % code)

        self._build_toolbar()
        self._build_table()
        self._build_bulk_panel()
        self._build_statusbar()
        self.reload()

    # -- changer de base --------------------------------------------------
    def change_database(self):
        """Rouvre l'ecran de connexion et reconstruit l'application sur la base
        choisie (le schema peut differer : on recree toute l'interface)."""
        if self._has_pending() and not messagebox.askyesno(
                APP_TITLE, "Des modifications ne sont pas enregistrees. "
                "Changer de base et les abandonner ?"):
            return
        start_cfg = getattr(self.repo, "cfg", None) or dict(article_db.DEFAULT_CONFIG)
        repo, cfg = prompt_connection(self.master, start_cfg)
        if repo is None:
            return
        try:
            self.repo.close()
        except Exception:                              # noqa: BLE001
            pass
        if not isinstance(repo, DemoRepository):
            try:
                save_config(self.config_path or _default_config_path(), cfg)
            except OSError:
                pass
        new_app = BulkEditorApp(self.master, repo, self.config_path)
        self.master._app = new_app                     # pour la fermeture propre
        self.destroy()

    # -- construction de l'interface -------------------------------------
    def _build_toolbar(self):
        bar = ttk.Frame(self)
        bar.pack(fill="x", pady=(0, 6))

        ttk.Label(bar, text="Recherche :").pack(side="left")
        self.search_var = tk.StringVar()
        ent = ttk.Entry(bar, textvariable=self.search_var, width=28)
        ent.pack(side="left", padx=(4, 4))
        ent.bind("<Return>", lambda e: self.reload())
        ttk.Button(bar, text="Filtrer", command=self.reload).pack(side="left")
        ttk.Button(bar, text="Tout afficher",
                   command=lambda: (self.search_var.set(""), self.reload())).pack(side="left", padx=4)

        ttk.Separator(bar, orient="vertical").pack(side="left", fill="y", padx=8)
        ttk.Button(bar, text="Changer de base…", command=self.change_database).pack(side="left", padx=2)
        ttk.Button(bar, text="Imprimer etiquettes...", command=self.print_labels).pack(side="left", padx=2)
        ttk.Button(bar, text="Importer Excel/CSV...", command=self.import_file).pack(side="left", padx=2)
        ttk.Button(bar, text="Exporter CSV...", command=self.export_csv).pack(side="left", padx=2)

        self.save_btn = ttk.Button(bar, text="Enregistrer (0)", command=self.commit_changes)
        self.save_btn.pack(side="right", padx=2)
        self.cancel_btn = ttk.Button(bar, text="Annuler les modifs", command=self.discard_changes)
        self.cancel_btn.pack(side="right", padx=2)

    def _build_table(self):
        wrap = ttk.Frame(self)
        wrap.pack(fill="both", expand=True)

        self.tree = ttk.Treeview(wrap, columns=self.columns, show="headings",
                                 selectmode="extended")
        widths = {Cols.REF: 90, Cols.DESIGNATION: 230, Cols.CODES_EQUIV: 180,
                  Cols.PV_HT: 90, Cols.PV_TTC: 90, Cols.PV_TTC_PROMO: 90,
                  Cols.PROMO_ACTIVE: 60,
                  Cols.TVA: 60, Cols.PA_HT: 90, Cols.QTE_CARTON: 80,
                  Cols.FAMILLE: 110}
        for c in self.columns:
            label = Cols.LABELS.get(c, self.tarif_labels.get(c, c))
            self.tree.heading(c, text=label,
                              command=lambda col=c: self._sort_by(col))
            is_num = c in Cols.NUMERIC or c.startswith("__tarif_")
            anchor = "e" if is_num else "w"
            self.tree.column(c, width=widths.get(c, 100), anchor=anchor, stretch=False)
        self.tree.tag_configure("modif", background="#fff3bf")     # jaune = modifie
        self.tree.tag_configure("editable_hint", background="#ffffff")

        vsb = ttk.Scrollbar(wrap, orient="vertical", command=self.tree.yview)
        hsb = ttk.Scrollbar(wrap, orient="horizontal", command=self.tree.xview)
        self.tree.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)
        self.tree.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")
        hsb.grid(row=1, column=0, sticky="ew")
        wrap.rowconfigure(0, weight=1)
        wrap.columnconfigure(0, weight=1)

        self.tree.bind("<Double-1>", self._on_double_click)

    def _build_bulk_panel(self):
        panel = ttk.LabelFrame(self, text="Operations en masse (sur la selection)", padding=8)
        panel.pack(fill="x", pady=(6, 0))

        # --- Prix de vente ---
        price = ttk.Frame(panel)
        price.grid(row=0, column=0, sticky="w", padx=(0, 16))
        ttk.Label(price, text="PRIX DE VENTE", font=("", 9, "bold")).grid(
            row=0, column=0, columnspan=4, sticky="w")
        ttk.Label(price, text="(HT et TTC = le meme prix ; la TVA n'est pas modifiee)",
                  foreground="#888").grid(row=1, column=0, columnspan=4, sticky="w")
        self.price_mode = tk.StringVar(value="set")
        modes = [("Fixer a", "set"), ("+ %", "inc_pct"), ("- %", "dec_pct"),
                 ("+ montant", "inc_amount"), ("- montant", "dec_amount"),
                 ("Arrondir seul.", "round")]
        self.price_combo = ttk.Combobox(price, state="readonly", width=14,
                                        values=[m[0] for m in modes])
        self.price_combo.current(0)
        self.price_combo.grid(row=2, column=0, columnspan=2, padx=2, pady=(4, 0), sticky="w")
        self._price_modes = dict(modes)
        self.price_value = tk.StringVar()
        ttk.Entry(price, textvariable=self.price_value, width=10).grid(
            row=2, column=2, padx=4, pady=(4, 0))
        ttk.Label(price, text="Arrondi :").grid(row=3, column=0, sticky="e", pady=(4, 0))
        self.round_combo = ttk.Combobox(price, state="readonly", width=10,
                                       values=[r[0] for r in ROUND_CHOICES])
        self.round_combo.current(0)
        self.round_combo.grid(row=3, column=1, sticky="w", pady=(4, 0))
        ttk.Button(price, text="Appliquer prix", command=self.apply_price).grid(
            row=3, column=2, columnspan=2, sticky="e", pady=(4, 0))

        # --- TVA (taux), modifiable independamment du prix ---
        if self.repo.has(Cols.TVA):
            ttk.Separator(price, orient="horizontal").grid(
                row=4, column=0, columnspan=4, sticky="ew", pady=6)
            ttk.Label(price, text="TVA %").grid(row=5, column=0, sticky="e")
            self.tva_value = tk.StringVar()
            ttk.Entry(price, textvariable=self.tva_value, width=8).grid(
                row=5, column=1, sticky="w")
            ttk.Button(price, text="Fixer la TVA", command=self.apply_tva).grid(
                row=5, column=2, columnspan=2, sticky="e")

        # --- Qte / carton ---
        if self.repo.has(Cols.QTE_CARTON):
            ttk.Separator(price, orient="horizontal").grid(
                row=6, column=0, columnspan=4, sticky="ew", pady=6)
            ttk.Label(price, text="Qte/carton").grid(row=7, column=0, sticky="e")
            self.qtec_value = tk.StringVar()
            ttk.Entry(price, textvariable=self.qtec_value, width=8).grid(
                row=7, column=1, sticky="w")
            ttk.Button(price, text="Fixer qte/carton", command=self.apply_qte_carton).grid(
                row=7, column=2, columnspan=2, sticky="e")

        # --- Prix promo ---
        if self.repo.has(Cols.PV_TTC_PROMO):
            ttk.Separator(price, orient="horizontal").grid(
                row=8, column=0, columnspan=4, sticky="ew", pady=6)
            ttk.Label(price, text="PRIX PROMO", font=("", 9, "bold")).grid(
                row=9, column=0, columnspan=4, sticky="w")
            ttk.Label(price, text="(active la promo ; HT et TTC = meme prix)",
                      foreground="#888").grid(row=10, column=0, columnspan=4, sticky="w")
            ttk.Label(price, text="Prix promo").grid(row=11, column=0, sticky="e", pady=(4, 0))
            self.promo_value = tk.StringVar()
            ttk.Entry(price, textvariable=self.promo_value, width=8).grid(
                row=11, column=1, sticky="w", pady=(4, 0))
            ttk.Button(price, text="Fixer promo", command=self.apply_promo).grid(
                row=11, column=2, columnspan=2, sticky="e", pady=(4, 0))
            self.promo_start = tk.StringVar()
            self.promo_end = tk.StringVar()
            if self.repo.has(Cols.PROMO_START) or self.repo.has(Cols.PROMO_END):
                dates = ttk.Frame(price)
                dates.grid(row=12, column=0, columnspan=4, sticky="w", pady=(2, 0))
                ttk.Label(dates, text="du").pack(side="left")
                ttk.Entry(dates, textvariable=self.promo_start, width=10).pack(side="left", padx=2)
                ttk.Label(dates, text="au").pack(side="left")
                ttk.Entry(dates, textvariable=self.promo_end, width=10).pack(side="left", padx=2)
                ttk.Label(dates, text="(JJ/MM/AAAA)", foreground="#888").pack(side="left")
            ttk.Button(price, text="Desactiver promo", command=self.disable_promo).grid(
                row=13, column=0, columnspan=4, sticky="w", pady=(4, 0))

        ttk.Separator(panel, orient="vertical").grid(row=0, column=1, sticky="ns", padx=8)

        # --- Codes equivalents (plusieurs codes-barres par article) ---
        if self.equiv_enabled:
            cb = ttk.Frame(panel)
            cb.grid(row=0, column=2, sticky="w", padx=(0, 16))
            ttk.Label(cb, text="CODES EQUIV.", font=("", 9, "bold")).grid(
                row=0, column=0, columnspan=3, sticky="w")
            ttk.Label(cb, text="(plusieurs codes possibles, separes par ;)",
                      foreground="#888").grid(row=1, column=0, columnspan=3, sticky="w")
            self.equiv_value = tk.StringVar()
            ttk.Entry(cb, textvariable=self.equiv_value, width=18).grid(
                row=2, column=0, columnspan=2, pady=2)
            ttk.Button(cb, text="Ajouter", width=8,
                       command=self.apply_equiv_add).grid(row=2, column=2, padx=2)
            ttk.Button(cb, text="Ajouter la reference", width=20,
                       command=self.add_ref_as_equiv).grid(
                row=3, column=0, columnspan=2, pady=2, sticky="w")
            ttk.Button(cb, text="Vider", width=8,
                       command=self.apply_equiv_clear).grid(row=3, column=2, padx=2)

        ttk.Separator(panel, orient="vertical").grid(row=0, column=3, sticky="ns", padx=8)

        # --- Reference ---
        ref = ttk.Frame(panel)
        ref.grid(row=0, column=4, sticky="w")
        ttk.Label(ref, text="REFERENCE", font=("", 9, "bold")).grid(
            row=0, column=0, columnspan=3, sticky="w")
        ttk.Label(ref, text="Chercher").grid(row=1, column=0, sticky="e")
        self.ref_find = tk.StringVar()
        ttk.Entry(ref, textvariable=self.ref_find, width=12).grid(row=1, column=1, pady=2)
        ttk.Label(ref, text="Remplacer").grid(row=2, column=0, sticky="e")
        self.ref_repl = tk.StringVar()
        ttk.Entry(ref, textvariable=self.ref_repl, width=12).grid(row=2, column=1, pady=2)
        ttk.Button(ref, text="Remplacer", command=self.apply_ref_replace).grid(
            row=1, column=2, rowspan=2, padx=4)
        ttk.Label(ref, text="(la reference est aussi le code scanne)",
                  foreground="#888").grid(row=3, column=0, columnspan=3, sticky="w")

        if not self.repo.has(Cols.REF):
            ref.grid_remove()

        # --- Famille ---
        if self.repo.has(Cols.FAMILLE) and getattr(self.repo, "has_famille_ref", lambda: False)():
            ttk.Separator(panel, orient="vertical").grid(row=0, column=5, sticky="ns", padx=8)
            fam = ttk.Frame(panel)
            fam.grid(row=0, column=6, sticky="w")
            ttk.Label(fam, text="FAMILLE", font=("", 9, "bold")).grid(
                row=0, column=0, columnspan=3, sticky="w")
            ttk.Label(fam, text="Existante :").grid(row=1, column=0, sticky="e")
            self.fam_combo = ttk.Combobox(fam, state="readonly", width=22)
            self.fam_combo.grid(row=1, column=1, pady=2)
            ttk.Button(fam, text="Affecter", width=9,
                       command=self.apply_famille_existing).grid(row=1, column=2, padx=2)
            ttk.Label(fam, text="Nouvelle :").grid(row=2, column=0, sticky="e")
            self.fam_new_code = tk.StringVar()
            self.fam_new_name = tk.StringVar()
            nf = ttk.Frame(fam)
            nf.grid(row=2, column=1, sticky="w", pady=2)
            ttk.Entry(nf, textvariable=self.fam_new_code, width=8).pack(side="left")
            ttk.Label(nf, text="code").pack(side="left", padx=(2, 6))
            ttk.Entry(nf, textvariable=self.fam_new_name, width=14).pack(side="left")
            ttk.Label(nf, text="nom").pack(side="left", padx=2)
            ttk.Button(fam, text="Creer & affecter", width=15,
                       command=self.apply_famille_new).grid(row=2, column=2, padx=2)
            self._refresh_famille_combo()

        # --- Tarifs (prix par type de tarif, en masse sur la selection) ---
        if self.tarif_types:
            ttk.Separator(panel, orient="vertical").grid(row=0, column=7, sticky="ns", padx=8)
            tar = ttk.Frame(panel)
            tar.grid(row=0, column=8, sticky="nw")
            ttk.Label(tar, text="TARIFS", font=("", 9, "bold")).grid(
                row=0, column=0, columnspan=3, sticky="w")
            ttk.Label(tar, text="Type :").grid(row=1, column=0, sticky="e", pady=2)
            self.tarif_bulk_combo = ttk.Combobox(
                tar, state="readonly", width=20,
                values=["%s — %s" % (c, n) for c, n in self.tarif_types])
            self.tarif_bulk_combo.current(0)
            self.tarif_bulk_combo.grid(row=1, column=1, columnspan=2, pady=2, sticky="w")
            self.tarif_bulk_mode = tk.StringVar(value="manual")
            ttk.Radiobutton(tar, text="Manuel", variable=self.tarif_bulk_mode,
                            value="manual").grid(row=2, column=0, columnspan=2, sticky="w",
                                                 pady=(4, 0))
            ttk.Radiobutton(tar, text="% du PA HT", variable=self.tarif_bulk_mode,
                            value="pct_pa").grid(row=2, column=2, sticky="w", pady=(4, 0))
            ttk.Label(tar, text="Valeur :").grid(row=3, column=0, sticky="e", pady=(4, 0))
            self.tarif_bulk_value = tk.StringVar()
            ttk.Entry(tar, textvariable=self.tarif_bulk_value, width=10).grid(
                row=3, column=1, padx=4, pady=(4, 0))
            ttk.Button(tar, text="Appliquer", command=self.apply_tarif_bulk).grid(
                row=3, column=2, pady=(4, 0))

    def _build_statusbar(self):
        self.status = tk.StringVar()
        bar = ttk.Frame(self)
        bar.pack(fill="x", pady=(6, 0))
        ttk.Label(bar, textvariable=self.status, anchor="w").pack(side="left")

    # -- chargement / affichage ------------------------------------------
    def _has_pending(self):
        return bool(self.pending or self.new_familles or self.pending_tarifs
                    or self.pending_equiv)

    def reload(self):
        if self._has_pending() and not messagebox.askyesno(
                APP_TITLE,
                "Des modifications ne sont pas enregistrees. "
                "Les abandonner et recharger ?"):
            return
        self.pending.clear()
        self.new_familles.clear()
        self.pending_tarifs.clear()
        self.pending_equiv.clear()
        try:
            self.repo.rollback()                 # annule toute ecriture non validee
        except Exception:                        # noqa: BLE001
            pass
        try:
            self.rows = self.repo.load(self.search_var.get())
        except DBError as exc:
            messagebox.showerror(APP_TITLE, str(exc))
            return
        refs = [r.get("__ref0__") for r in self.rows if r.get("__ref0__")]
        try:
            self.tarif_data = self.repo.load_tarifs(refs)
        except Exception:                        # noqa: BLE001
            self.tarif_data = {}
        try:
            self.equiv_data = (self.repo.load_equiv(refs)
                               if self.equiv_enabled else {})
        except Exception:                        # noqa: BLE001
            self.equiv_data = {}
        self._populate()
        self._refresh_famille_combo()
        self._update_save_button()
        self._update_sort_indicators()

    def _populate(self):
        self._apply_sort()
        self.tree.delete(*self.tree.get_children())
        self.row_by_iid.clear()
        for rec in self.rows:
            iid = self._insert_row(rec)
            self.row_by_iid[iid] = rec
        try:
            total = self.repo.count()
        except Exception:                               # noqa: BLE001
            total = len(self.rows)
        self.status.set("%d article(s) affiche(s) sur %d  —  table %s"
                        % (len(self.rows), total, self.repo.table))

    def _codes_for(self, ref0):
        """Codes equivalents courants d'un article (en attente sinon en base)."""
        if ref0 in self.pending_equiv:
            return self.pending_equiv[ref0]
        return self.equiv_data.get(ref0, [])

    def _row_values(self, rec):
        vals = []
        ref0 = rec.get("__ref0__")
        for c in self.columns:
            if c.startswith("__tarif_"):
                type_code = c[8:-2]
                v = self.pending_tarifs.get(ref0, {}).get(
                    type_code, self.tarif_data.get(ref0, {}).get(type_code))
                vals.append(fmt_price(v) if v is not None else "")
            elif c == Cols.CODES_EQUIV:
                vals.append(join_codes(self._codes_for(ref0)))
            elif c in Cols.BOOL_FIELDS:
                vals.append("Oui" if parse_bool(rec.get(c)) else "")
            elif c in Cols.NUMERIC:
                vals.append(fmt_price(rec.get(c)))
            else:
                v = rec.get(c)
                vals.append("" if v is None else str(v))
        return vals

    def _insert_row(self, rec):
        return self.tree.insert("", "end", values=self._row_values(rec))

    def _refresh_row(self, iid, rec):
        self.tree.item(iid, values=self._row_values(rec))
        ref0 = rec.get("__ref0__")
        modified = (ref0 in self.pending or ref0 in self.pending_tarifs
                    or ref0 in self.pending_equiv)
        self.tree.item(iid, tags=("modif",) if modified else ())

    # -- edition d'une cellule (double-clic) -----------------------------
    def _on_double_click(self, event):
        if self.tree.identify_region(event.x, event.y) != "cell":
            return
        iid = self.tree.identify_row(event.y)
        col_id = self.tree.identify_column(event.x)
        if not iid or not col_id:
            return
        col_idx = int(col_id[1:]) - 1
        logical = self.columns[col_idx]
        rec = self.row_by_iid[iid]

        # -- colonne tarif (prix par type de tarif) -----------------------
        if logical.startswith("__tarif_"):
            type_code = logical[8:-2]
            ref0 = rec.get("__ref0__")
            old = self.pending_tarifs.get(ref0, {}).get(
                type_code, self.tarif_data.get(ref0, {}).get(type_code))
            box = self.tree.bbox(iid, col_id)
            if not box:                       # cellule hors champ : on la rend visible
                self.tree.see(iid)
                self.update_idletasks()
                box = self.tree.bbox(iid, col_id)
            if not box:
                return
            x, y, w, h = box
            edit = tk.Entry(self.tree)
            edit.insert(0, fmt_price(old) if old is not None else "")
            edit.select_range(0, "end")
            edit.focus_set()
            edit.place(x=x, y=y, width=w, height=h)
            self.status.set("Tarif : tapez un prix (ex 12,50) ou une marge en %% "
                            "du PA HT (ex 20%%), puis Entree.")

            def commit_tarif(_=None, tc=type_code):
                new = edit.get()
                edit.destroy()
                self._set_tarif_cell(iid, rec, tc, new)

            edit.bind("<Return>", commit_tarif)
            edit.bind("<Escape>", lambda e: edit.destroy())
            edit.bind("<FocusOut>", commit_tarif)
            return

        # -- colonne codes equivalents (liste editable, separateur ';') ---
        if logical == Cols.CODES_EQUIV:
            ref0 = rec.get("__ref0__")
            box = self.tree.bbox(iid, col_id)
            if not box:                       # cellule hors champ : on la rend visible
                self.tree.see(iid)
                self.update_idletasks()
                box = self.tree.bbox(iid, col_id)
            if not box:
                return
            x, y, w, h = box
            edit = tk.Entry(self.tree)
            edit.insert(0, join_codes(self._codes_for(ref0)))
            edit.select_range(0, "end")
            edit.focus_set()
            edit.place(x=x, y=y, width=max(w, 240), height=h)
            self.status.set("Codes equivalents : plusieurs codes separes par ';' "
                            "(vider = supprimer tous les codes), puis Entree.")

            def commit_equiv(_=None):
                new = edit.get()
                edit.destroy()
                self._set_equiv_cell(iid, rec, new)

            edit.bind("<Return>", commit_equiv)
            edit.bind("<Escape>", lambda e: edit.destroy())
            edit.bind("<FocusOut>", commit_equiv)
            return

        if logical not in Cols.EDITABLE:
            messagebox.showinfo(APP_TITLE, "Cette colonne n'est pas modifiable ici.")
            return
        x, y, w, h = self.tree.bbox(iid, col_id)
        old = rec.get(logical)
        if logical in Cols.BOOL_FIELDS:
            prefill = "Oui" if parse_bool(old) else "Non"
        elif logical in Cols.NUMERIC:
            prefill = "" if old is None else fmt_price(old)
        else:
            prefill = "" if old is None else str(old)
        edit = tk.Entry(self.tree)
        edit.insert(0, prefill)
        edit.select_range(0, "end")
        edit.focus_set()
        edit.place(x=x, y=y, width=w, height=h)

        def commit(_=None):
            new = edit.get()
            edit.destroy()
            if logical == Cols.FAMILLE:
                self._edit_famille_inline(iid, rec, new)
            else:
                self._set_cell(iid, rec, logical, new)

        edit.bind("<Return>", commit)
        edit.bind("<Escape>", lambda e: edit.destroy())
        edit.bind("<FocusOut>", commit)

    def _set_cell(self, iid, rec, logical, new_value):
        """Valide et applique une nouvelle valeur de cellule (en attente)."""
        if logical in Cols.BOOL_FIELDS:
            b = parse_bool(new_value)
            if new_value.strip() != "" and b is None:
                messagebox.showwarning(APP_TITLE, "Repondez par Oui ou Non.")
                return
            value = b if b is not None else 0
        elif logical in Cols.NUMERIC:
            num = parse_number(new_value)
            if new_value.strip() != "" and num is None:
                messagebox.showwarning(APP_TITLE, "Valeur numerique invalide : %r" % new_value)
                return
            value = num
            # le prix de vente HT et TTC restent identiques (pas la TVA)
            if logical in Cols.PRICE_FIELDS:
                self._sync_price(rec, logical, value)
            elif logical in Cols.PROMO_PRICE_FIELDS:
                self._sync_promo(rec, logical, value)
        else:
            value = new_value.strip() or None
            real = self.repo.real(logical)
            maxb = self.repo.maxlen.get(real, Cols.DEFAULT_MAX_LEN.get(logical, 255))
            if value and encoded_len(value, self.repo.codec) > maxb:
                messagebox.showwarning(
                    APP_TITLE, "Trop long pour %s (max %d caracteres). Sera tronque."
                    % (Cols.LABELS.get(logical, logical), maxb))
            if logical == Cols.REF and value and value != rec.get(Cols.REF):
                if not self._warn_ref_rename():
                    return
        rec[logical] = value
        self._stage(rec, logical, value)
        self._refresh_row(iid, rec)
        self._update_save_button()

    def _set_tarif_cell(self, iid, rec, type_code, new_value):
        """Valide et met en attente une modification de prix tarif.

        Deux facons de saisir, au choix, directement dans la cellule :
          * un nombre simple  (ex : 12,50)  -> prix MANUEL ;
          * un nombre suivi de %  (ex : 20%) -> MARGE en % du PRIX D'ACHAT HT
            -> prix = PA_HT x (1 + 20/100). Necessite un PA HT sur l'article.
        """
        new_value = new_value.strip()
        ref0 = rec.get("__ref0__")
        if new_value == "":
            if ref0 in self.pending_tarifs:
                self.pending_tarifs[ref0].pop(type_code, None)
                if not self.pending_tarifs[ref0]:
                    del self.pending_tarifs[ref0]
            self._refresh_row(iid, rec)
            self._update_save_button()
            return
        if new_value.endswith("%"):
            pct = parse_number(new_value[:-1])
            if pct is None:
                messagebox.showwarning(APP_TITLE, "Pourcentage invalide : %r" % new_value)
                return
            pa = parse_number(rec.get(Cols.PA_HT))
            if pa is None:
                messagebox.showwarning(
                    APP_TITLE, "Pas de prix d'achat HT sur cet article : "
                    "impossible de calculer un %% (saisissez un prix manuel).")
                return
            val = round(pa * (1 + pct / 100.0), 2)
        else:
            val = parse_number(new_value)
            if val is None:
                messagebox.showwarning(APP_TITLE, "Valeur numerique invalide : %r" % new_value)
                return
            val = round(val, 2)
        self.pending_tarifs.setdefault(ref0, {})[type_code] = val
        self._refresh_row(iid, rec)
        self._update_save_button()

    def _set_equiv_cell(self, iid, rec, new_value):
        """Valide et met en attente la NOUVELLE liste de codes equivalents.

        La saisie remplace la liste complete : '123 ; 456' = ces deux codes,
        chaine vide = plus aucun code pour cet article.
        """
        ref0 = rec.get("__ref0__")
        codes = split_codes(new_value)
        maxb = getattr(self.repo, "equiv_code_max", Cols.EQUIV_CODE_LEN)
        too_long = [c for c in codes if encoded_len(c, self.repo.codec) > maxb]
        if too_long:
            messagebox.showwarning(
                APP_TITLE, "Code(s) trop long(s) (max %d caracteres), "
                "sera/seront tronque(s) : %s" % (maxb, ", ".join(too_long)))
        if codes == list(self.equiv_data.get(ref0, [])):
            self.pending_equiv.pop(ref0, None)   # revenu a l'etat de la base
        else:
            self.pending_equiv[ref0] = codes
        self._refresh_row(iid, rec)
        self._update_save_button()

    def _sync_price(self, rec, logical, value):
        """Maintient PRIXVENTEHT et PRIXVENTETTC IDENTIQUES.

        Dans la base PRIME, le prix de vente HT et le prix de vente TTC
        contiennent la MEME valeur (le prix saisi) ; la TVA est portee a part
        par TAUX_TVA. On recopie donc simplement la valeur sur l'autre champ,
        sans aucun calcul base sur la TVA (ce qui evitait de fausser le taux)."""
        for other in Cols.PRICE_FIELDS:
            if other != logical and self.repo.has(other):
                rec[other] = value
                self._stage(rec, other, value)

    def _sync_promo(self, rec, logical, value):
        """Maintient PRIXHTPROMO et PRIXTTCPROMO identiques et active/desactive
        automatiquement la promo (ACTIVEPROMO) selon qu'un prix est saisi."""
        for other in Cols.PROMO_PRICE_FIELDS:
            if other != logical and self.repo.has(other):
                rec[other] = value
                self._stage(rec, other, value)
        if self.repo.has(Cols.PROMO_ACTIVE):
            active = 1 if value not in (None, "") else 0
            rec[Cols.PROMO_ACTIVE] = active
            self._stage(rec, Cols.PROMO_ACTIVE, active)

    _ref_warned = False

    def _warn_ref_rename(self):
        if not BulkEditorApp._ref_warned:
            BulkEditorApp._ref_warned = True
            return messagebox.askyesno(
                APP_TITLE,
                "Vous modifiez une REFERENCE article (REF_ART).\n\n"
                "La reference est la cle de l'article et peut etre utilisee par "
                "les lignes de pieces (stock, ventes). Selon votre base, la "
                "renommer peut etre refuse ou necessiter une mise a jour liee.\n\n"
                "Continuer quand meme ?")
        return True

    # -- gestion des modifications en attente ----------------------------
    def _stage(self, rec, logical, value):
        ref0 = rec.get("__ref0__")
        self.pending.setdefault(ref0, {})[logical] = value

    def _update_save_button(self):
        n_tar = sum(len(v) for v in self.pending_tarifs.values())
        extra_tar = (" +%d tarif(s)" % n_tar) if n_tar else ""
        extra_fam = (" +%d fam." % len(self.new_familles)) if self.new_familles else ""
        extra_eq = (" +%d codes" % len(self.pending_equiv)) if self.pending_equiv else ""
        self.save_btn.config(
            text="Enregistrer (%d)%s%s%s" % (len(self.pending), extra_tar,
                                             extra_fam, extra_eq))

    def discard_changes(self):
        if not self._has_pending():
            return
        n_total = (len(self.pending) + len(self.pending_equiv)
                   + sum(len(v) for v in self.pending_tarifs.values()))
        if messagebox.askyesno(APP_TITLE, "Abandonner les %d modification(s) en attente ?"
                               % n_total):
            self.reload()

    def commit_changes(self):
        if not self._has_pending():
            messagebox.showinfo(APP_TITLE, "Aucune modification a enregistrer.")
            return
        changes = [{"ref0": ref0, "values": vals} for ref0, vals in self.pending.items()]
        new_fam = [(code, name, tva) for code, (name, tva) in self.new_familles.items()]
        tarif_changes = [(ref0, tc, price)
                         for ref0, tmap in self.pending_tarifs.items()
                         for tc, price in tmap.items()]
        equiv_changes = list(self.pending_equiv.items())
        detail = self._summary(changes)
        if new_fam:
            detail += "\n\nNouvelles familles : " + ", ".join(
                "%s (%s)" % (c, n) for c, n, _ in new_fam)
        if tarif_changes:
            detail += "\n\nTarifs modifies : %d prix" % len(tarif_changes)
        if equiv_changes:
            detail += "\n\nCodes equivalents modifies : %d article(s)" % len(equiv_changes)
        n_items = len(changes) + len(tarif_changes) + len(equiv_changes)
        if not messagebox.askyesno(APP_TITLE,
                                   "Enregistrer %d modification(s) ?\n\n%s"
                                   % (n_items, detail)):
            return
        try:
            n = self.repo.update_rows(changes, new_familles=new_fam)
            if tarif_changes:
                self.repo.update_tarifs(tarif_changes)
            if equiv_changes:
                self.repo.update_equiv(equiv_changes)
            self.repo.commit()
        except DBError as exc:
            self.repo.rollback()
            messagebox.showerror(APP_TITLE, "Echec — rien n'a ete enregistre.\n\n%s" % exc)
            return
        info = "%d article(s) enregistre(s)" % n
        if new_fam:
            info += " + %d famille(s)" % len(new_fam)
        if tarif_changes:
            info += " + %d tarif(s)" % len(tarif_changes)
        if equiv_changes:
            info += " + codes equiv. de %d article(s)" % len(equiv_changes)
        messagebox.showinfo(APP_TITLE, info + ".")
        self.reload()

    def _summary(self, changes, limit=12):
        lines = []
        for ch in changes[:limit]:
            parts = ", ".join("%s=%s" % (Cols.LABELS.get(k, k),
                                         fmt_price(v) if k in Cols.NUMERIC else v)
                              for k, v in ch["values"].items())
            lines.append("  %s : %s" % (ch["ref0"], parts))
        if len(changes) > limit:
            lines.append("  ... (%d de plus)" % (len(changes) - limit))
        return "\n".join(lines)

    # -- operations en masse ---------------------------------------------
    def _selected_recs(self):
        recs = [self.row_by_iid[i] for i in self.tree.selection()]
        if not recs:
            messagebox.showinfo(APP_TITLE, "Selectionnez d'abord une ou plusieurs lignes "
                                "(Ctrl+clic / Maj+clic).")
        return recs

    def apply_price(self):
        recs = self._selected_recs()
        if not recs:
            return
        mode = self._price_modes[self.price_combo.get()]
        round_to = dict(ROUND_CHOICES)[self.round_combo.get()]
        val = parse_number(self.price_value.get(), 0.0) or 0.0
        if mode != "round" and self.price_value.get().strip() == "" and mode == "set":
            messagebox.showwarning(APP_TITLE, "Indiquez la valeur du prix.")
            return
        op = PriceOp(mode=mode, value=val, round_to=round_to)
        # le prix de vente est unique : on prend un champ de reference present
        col = Cols.PV_HT if self.repo.has(Cols.PV_HT) else Cols.PV_TTC
        n = 0
        for rec in recs:
            new = op.apply(rec.get(col))
            if new is None:
                continue
            rec[col] = new
            self._stage(rec, col, new)
            self._sync_price(rec, col, new)   # recopie sur l'autre champ (HT=TTC)
            n += 1
        self._refresh_all_selected(recs)
        self.status.set("Prix de vente applique a %d article(s)." % n)

    def apply_tva(self):
        recs = self._selected_recs()
        if not recs:
            return
        tva = parse_number(self.tva_value.get())
        if tva is None:
            messagebox.showwarning(APP_TITLE, "Indiquez un taux de TVA (ex : 19).")
            return
        for rec in recs:
            rec[Cols.TVA] = tva
            self._stage(rec, Cols.TVA, tva)
        self._refresh_all_selected(recs)
        self.status.set("TVA %s%% appliquee a %d article(s)." % (fmt_price(tva), len(recs)))

    def apply_qte_carton(self):
        recs = self._selected_recs()
        if not recs:
            return
        qte = parse_number(self.qtec_value.get())
        if qte is None:
            messagebox.showwarning(APP_TITLE, "Indiquez une quantite par carton (ex : 12).")
            return
        for rec in recs:
            rec[Cols.QTE_CARTON] = qte
            self._stage(rec, Cols.QTE_CARTON, qte)
        self._refresh_all_selected(recs)
        self.status.set("Qte/carton %s appliquee a %d article(s)."
                        % (fmt_price(qte), len(recs)))

    def apply_promo(self):
        recs = self._selected_recs()
        if not recs:
            return
        val = parse_number(self.promo_value.get())
        if val is None:
            messagebox.showwarning(APP_TITLE, "Indiquez un prix promo (ex : 6,90).")
            return
        # dates optionnelles
        from editor_logic import parse_date
        start = end = None
        try:
            if getattr(self, "promo_start", None) is not None:
                start = self.promo_start.get().strip()
                parse_date(start)          # validation (leve si invalide)
            if getattr(self, "promo_end", None) is not None:
                end = self.promo_end.get().strip()
                parse_date(end)
        except ValueError as exc:
            messagebox.showwarning(APP_TITLE, str(exc))
            return
        for rec in recs:
            rec[Cols.PV_TTC_PROMO] = val
            self._stage(rec, Cols.PV_TTC_PROMO, val)
            self._sync_promo(rec, Cols.PV_TTC_PROMO, val)
            if start and self.repo.has(Cols.PROMO_START):
                self._stage(rec, Cols.PROMO_START, start)
            if end and self.repo.has(Cols.PROMO_END):
                self._stage(rec, Cols.PROMO_END, end)
        self._refresh_all_selected(recs)
        self.status.set("Prix promo %s applique (promo activee) a %d article(s)."
                        % (fmt_price(val), len(recs)))

    def disable_promo(self):
        recs = self._selected_recs()
        if not recs:
            return
        for rec in recs:
            if self.repo.has(Cols.PROMO_ACTIVE):
                rec[Cols.PROMO_ACTIVE] = 0
                self._stage(rec, Cols.PROMO_ACTIVE, 0)
        self._refresh_all_selected(recs)
        self.status.set("Promo desactivee pour %d article(s)." % len(recs))

    # -- codes equivalents (en masse) --------------------------------------
    def _add_equiv_codes(self, rec, codes):
        """Ajoute des codes a la liste d'un article (sans doublons)."""
        ref0 = rec.get("__ref0__")
        current = list(self._codes_for(ref0))
        changed = False
        for c in codes:
            if c and c not in current:
                current.append(c)
                changed = True
        if changed:
            self.pending_equiv[ref0] = current
        return changed

    def apply_equiv_add(self):
        recs = self._selected_recs()
        if not recs:
            return
        codes = split_codes(self.equiv_value.get())
        if not codes:
            messagebox.showwarning(
                APP_TITLE, "Indiquez au moins un code (plusieurs codes : "
                "separez-les par ';').")
            return
        n = sum(1 for rec in recs if self._add_equiv_codes(rec, codes))
        self._refresh_all_selected(recs)
        self.status.set("%d code(s) equivalent(s) ajoute(s) sur %d article(s)."
                        % (len(codes), n))

    def add_ref_as_equiv(self):
        recs = self._selected_recs()
        if not recs:
            return
        n = 0
        for rec in recs:
            ref = (rec.get(Cols.REF) or "").strip()
            if ref and self._add_equiv_codes(rec, [ref]):
                n += 1
        self._refresh_all_selected(recs)
        self.status.set("Reference ajoutee aux codes equivalents de %d article(s)." % n)

    def apply_equiv_clear(self):
        recs = self._selected_recs()
        if not recs:
            return
        n = 0
        for rec in recs:
            ref0 = rec.get("__ref0__")
            if self._codes_for(ref0):
                self.pending_equiv[ref0] = []
                n += 1
            else:
                self.pending_equiv.pop(ref0, None)
        self._refresh_all_selected(recs)
        self.status.set("Codes equivalents vides pour %d article(s)." % n)

    def apply_ref_replace(self):
        if not self.repo.has(Cols.REF):
            return
        recs = self._selected_recs()
        if not recs:
            return
        find = self.ref_find.get()
        if not find:
            messagebox.showwarning(APP_TITLE, "Indiquez le texte a chercher dans la reference.")
            return
        if not self._warn_ref_rename():
            return
        op = TextOp("replace", value=self.ref_repl.get(), find=find)
        changed = 0
        for rec in recs:
            old = rec.get(Cols.REF)
            new = op.apply(old)
            if new and new != old:
                if self.repo.ref_exists(new) and new not in (r.get(Cols.REF) for r in recs):
                    messagebox.showwarning(APP_TITLE,
                                           "La reference '%s' existe deja. Operation interrompue." % new)
                    return
                rec[Cols.REF] = new
                self._stage(rec, Cols.REF, new)
                changed += 1
        self._refresh_all_selected(recs)
        self.status.set("Reference modifiee pour %d article(s)." % changed)

    # -- famille ----------------------------------------------------------
    def _famille_list(self):
        """Familles existantes en base + celles en attente de creation."""
        items = list(self.repo.list_familles())
        existing = {c for c, _ in items}
        for code, (name, _tva) in self.new_familles.items():
            if code not in existing:
                items.append((code, (name or "") + "  (nouvelle)"))
        return sorted(items, key=lambda t: str(t[0]))

    def _refresh_famille_combo(self):
        if not hasattr(self, "fam_combo"):
            return
        self._fam_values = self._famille_list()
        self.fam_combo["values"] = ["%s — %s" % (c, n) for c, n in self._fam_values]

    def _assign_famille(self, code, recs):
        for rec in recs:
            rec[Cols.FAMILLE] = code
            self._stage(rec, Cols.FAMILLE, code)
        self._refresh_all_selected(recs)

    def apply_famille_existing(self):
        recs = self._selected_recs()
        if not recs:
            return
        sel = self.fam_combo.current()
        if sel < 0:
            messagebox.showwarning(APP_TITLE, "Choisissez une famille dans la liste.")
            return
        code = self._fam_values[sel][0]
        self._assign_famille(code, recs)
        self.status.set("Famille '%s' affectee a %d article(s)." % (code, len(recs)))

    def apply_famille_new(self):
        recs = self._selected_recs()
        if not recs:
            return
        code = self.fam_new_code.get().strip()
        name = self.fam_new_name.get().strip()
        if not code or not name:
            messagebox.showwarning(APP_TITLE,
                                   "Indiquez le CODE et le NOM de la nouvelle famille.")
            return
        if self.repo.famille_exists(code):
            if not messagebox.askyesno(
                    APP_TITLE, "La famille '%s' existe deja. L'affecter quand meme ?" % code):
                return
        else:
            self.new_familles[code] = (name, None)
            self._refresh_famille_combo()
        self._assign_famille(code, recs)
        self.status.set("Famille '%s' (%s) creee et affectee a %d article(s) "
                        "— a confirmer par Enregistrer." % (code, name, len(recs)))

    def _edit_famille_inline(self, iid, rec, new_code):
        """Edition de CODEFAMILLE par double-clic : verifie l'existence, propose
        de creer la famille (code + nom) si elle n'existe pas."""
        new_code = (new_code or "").strip()
        if not new_code:
            rec[Cols.FAMILLE] = None
            self._stage(rec, Cols.FAMILLE, None)
            self._refresh_row(iid, rec)
            self._update_save_button()
            return
        if not (self.repo.famille_exists(new_code) or new_code in self.new_familles):
            name = simpledialog.askstring(
                APP_TITLE, "La famille '%s' n'existe pas.\nNom de la nouvelle famille "
                "(laisser vide pour annuler) :" % new_code, parent=self.master)
            if not name:
                return
            self.new_familles[new_code] = (name.strip(), None)
            self._refresh_famille_combo()
        rec[Cols.FAMILLE] = new_code
        self._stage(rec, Cols.FAMILLE, new_code)
        self._refresh_row(iid, rec)
        self._update_save_button()

    def apply_tarif_bulk(self):
        """Operation en masse : fixer le prix d'un type de tarif (manuel ou % du PA HT)."""
        recs = self._selected_recs()
        if not recs:
            return
        idx = self.tarif_bulk_combo.current()
        if idx < 0:
            messagebox.showwarning(APP_TITLE, "Choisissez un type de tarif.")
            return
        type_code, type_name = self.tarif_types[idx]
        val = parse_number(self.tarif_bulk_value.get())
        if val is None:
            messagebox.showwarning(APP_TITLE, "Indiquez une valeur numerique.")
            return
        mode = self.tarif_bulk_mode.get()
        n = 0
        for rec in recs:
            if mode == "pct_pa":
                pa = parse_number(rec.get(Cols.PA_HT))
                if pa is None:
                    continue
                price = round(pa * (1 + val / 100.0), 2)
            else:
                price = round(val, 2)
            self.pending_tarifs.setdefault(rec.get("__ref0__"), {})[type_code] = price
            n += 1
        self._refresh_all_selected(recs)
        if mode == "pct_pa":
            self.status.set("Tarif '%s' = PA HT +%g%% applique a %d article(s)."
                            % (type_name, val, n))
        else:
            self.status.set("Tarif '%s' fixe a %.2f pour %d article(s)."
                            % (type_name, price, n))

    def _refresh_all_selected(self, recs):
        for iid in self.tree.selection():
            self._refresh_row(iid, self.row_by_iid[iid])
        self._update_save_button()

    # -- tri par colonne --------------------------------------------------
    def _sort_by(self, col):
        """Clic sur un en-tete de colonne : bascule tri asc / desc."""
        if self._sort_col == col:
            self._sort_rev = not self._sort_rev
        else:
            self._sort_col = col
            self._sort_rev = False
        self._apply_sort()
        self._populate()
        self._update_sort_indicators()

    def _apply_sort(self):
        """Trie self.rows en memoire selon la colonne active (sans repeupler)."""
        if self._sort_col is None:
            return
        col = self._sort_col
        is_num = col in Cols.NUMERIC or col.startswith("__tarif_")

        def key(rec):
            if col.startswith("__tarif_"):
                type_code = col[8:-2]
                v = self.tarif_data.get(rec.get("__ref0__") or "", {}).get(type_code)
            elif col == Cols.CODES_EQUIV:
                v = join_codes(self._codes_for(rec.get("__ref0__")))
            else:
                v = rec.get(col)
            if is_num:
                try:
                    return (0, float(v)) if v is not None else (1, 0.0)
                except (ValueError, TypeError):
                    return (1, 0.0)
            return str(v or "").lower()

        try:
            self.rows.sort(key=key, reverse=self._sort_rev)
        except Exception:             # noqa: BLE001
            pass

    def _update_sort_indicators(self):
        """Met a jour les fleches ▲/▼ dans les en-tetes de colonnes."""
        if not hasattr(self, "tree"):
            return
        for c in self.columns:
            base = Cols.LABELS.get(c, self.tarif_labels.get(c, c))
            if c == self._sort_col:
                label = base + (" ▼" if self._sort_rev else " ▲")
            else:
                label = base
            self.tree.heading(c, text=label)

    # -- impression d'etiquettes -----------------------------------------
    def _label_items(self, recs):
        """Construit les etiquettes (label_print.LabelItem) des articles."""
        items = []
        for rec in recs:
            ref0 = rec.get("__ref0__")
            codes = self._codes_for(ref0)
            barcode = codes[0] if codes else (rec.get(Cols.REF) or "")
            price = parse_number(rec.get(Cols.PV_TTC))
            if price is None:
                price = parse_number(rec.get(Cols.PV_HT))
            promo = None
            if parse_bool(rec.get(Cols.PROMO_ACTIVE)):
                promo = parse_number(rec.get(Cols.PV_TTC_PROMO))
            items.append(label_print.LabelItem(
                designation=rec.get(Cols.DESIGNATION) or "",
                reference=rec.get(Cols.REF) or "",
                barcode=str(barcode),
                price=price, promo=promo))
        return items

    def print_labels(self):
        recs = self._selected_recs()
        if not recs:
            return
        items = self._label_items(recs)
        dlg = LabelPrintDialog(self.master, items)
        self.master.wait_window(dlg)
        if dlg.printed:
            self.status.set("%d etiquette(s) envoyee(s) a l'impression."
                            % dlg.printed)

    # -- import / export --------------------------------------------------
    def export_csv(self):
        path = filedialog.asksaveasfilename(
            title="Exporter la vue en CSV", defaultextension=".csv",
            filetypes=[("CSV", "*.csv")])
        if not path:
            return
        with open(path, "w", newline="", encoding="utf-8-sig") as fh:
            w = csv.writer(fh, delimiter=";")
            w.writerow([Cols.LABELS.get(c, self.tarif_labels.get(c, c))
                        for c in self.columns])
            for rec in self.rows:
                w.writerow(self._row_values(rec))
        messagebox.showinfo(APP_TITLE, "Export termine :\n%s" % path)

    def import_file(self):
        """Importe un fichier Excel/CSV pour mettre a jour prix/code-barres par reference."""
        path = filedialog.askopenfilename(
            title="Fichier de mise a jour (Excel/CSV)",
            filetypes=[("Excel/CSV", "*.xlsx *.xls *.csv"), ("Tous", "*.*")])
        if not path:
            return
        try:
            rows = _read_tabular(path)
        except Exception as exc:                        # noqa: BLE001
            messagebox.showerror(APP_TITLE, "Lecture impossible : %s" % exc)
            return
        if not rows:
            messagebox.showwarning(APP_TITLE, "Fichier vide.")
            return
        dlg = ImportMappingDialog(self.master, rows[0].keys())
        self.master.wait_window(dlg)
        if not dlg.result:
            return
        m = dlg.result    # {"ref": col, "pv_ht": col|None, "pv_ttc": col|None, "cb": col|None}
        index = {r.get("__ref0__"): r for r in self.rows}
        # construit aussi un index global (la vue peut etre filtree)
        applied, missing = 0, 0
        updates = {}
        equiv_updates = {}   # ref -> [codes] (colonne 'codes equivalents')
        for row in rows:
            ref = str(row.get(m["ref"], "")).strip()
            if not ref:
                continue
            vals = {}
            if m.get("pv_ht") and row.get(m["pv_ht"]) not in (None, ""):
                vals[Cols.PV_HT] = parse_number(row[m["pv_ht"]])
            if m.get("pv_ttc") and row.get(m["pv_ttc"]) not in (None, ""):
                vals[Cols.PV_TTC] = parse_number(row[m["pv_ttc"]])
            if m.get("cb") and row.get(m["cb"]) not in (None, ""):
                codes = split_codes(row[m["cb"]])
                if codes and self.equiv_enabled:
                    equiv_updates[ref] = codes
            if vals:
                updates[ref] = vals
        # Applique aux lignes visibles ; pour les autres, stage direct par ref.
        for ref in set(updates) | set(equiv_updates):
            rec = index.get(ref)
            vals = updates.get(ref) or {}
            if rec is not None:
                for k, v in vals.items():
                    rec[k] = v
                    self._stage(rec, k, v)
                if ref in equiv_updates:
                    self._add_equiv_codes(rec, equiv_updates[ref])
                applied += 1
            else:
                # pas dans la vue courante : on programme quand meme la modif
                if vals:
                    self.pending.setdefault(ref, {}).update(vals)
                if ref in equiv_updates:
                    # ajoute aux codes deja en base (l'ecriture REMPLACE la liste)
                    try:
                        existing = (self.repo.load_equiv([ref]) or {}).get(ref, [])
                    except Exception:                   # noqa: BLE001
                        existing = []
                    merged = list(existing) + [c for c in equiv_updates[ref]
                                               if c not in existing]
                    self.pending_equiv[ref] = merged
                missing += 1
        self._populate_keep_pending()
        self._update_save_button()
        messagebox.showinfo(
            APP_TITLE,
            "Import preparé : %d ligne(s) sur des articles visibles, %d sur "
            "des articles hors vue.\nVerifiez puis cliquez 'Enregistrer'."
            % (applied, missing))

    def _populate_keep_pending(self):
        self._populate()
        for iid, rec in self.row_by_iid.items():
            ref0 = rec.get("__ref0__")
            if (ref0 in self.pending or ref0 in self.pending_tarifs
                    or ref0 in self.pending_equiv):
                self._refresh_row(iid, rec)
        self._update_sort_indicators()


# --------------------------------------------------------------------------- #
#  Dialogue de correspondance des colonnes pour l'import
# --------------------------------------------------------------------------- #
class LabelPrintDialog(tk.Toplevel):
    """Choix du modele d'etiquette, de l'imprimante et du nombre de copies,
    avec APERCU a l'ecran, puis impression directe."""

    def __init__(self, master, items):
        super().__init__(master)
        self.title("Imprimer des etiquettes")
        self.resizable(False, False)
        self.items = items
        self.printed = 0

        frm = ttk.Frame(self, padding=12)
        frm.pack(fill="both", expand=True)

        ttk.Label(frm, text="%d article(s) selectionne(s)" % len(items),
                  font=("", 10, "bold")).grid(row=0, column=0, columnspan=2,
                                              sticky="w", pady=(0, 8))

        # modele
        ttk.Label(frm, text="Modele :").grid(row=1, column=0, sticky="w")
        self.models = label_print.LABEL_MODELS
        self.model_combo = ttk.Combobox(frm, state="readonly", width=48,
                                        values=[m.name for m in self.models])
        self.model_combo.current(0)
        self.model_combo.grid(row=1, column=1, sticky="w", pady=2)
        self.model_combo.bind("<<ComboboxSelected>>", lambda e: self._draw_preview())

        # imprimante
        ttk.Label(frm, text="Imprimante :").grid(row=2, column=0, sticky="w")
        printers = label_print.list_printers()
        default = label_print.default_printer()
        if default and default not in printers:
            printers.insert(0, default)
        self.printer_combo = ttk.Combobox(frm, state="readonly", width=48,
                                          values=printers or ["(aucune imprimante detectee)"])
        if printers:
            self.printer_combo.current(printers.index(default) if default in printers else 0)
        else:
            self.printer_combo.current(0)
        self.printer_combo.grid(row=2, column=1, sticky="w", pady=2)

        # copies
        ttk.Label(frm, text="Copies / article :").grid(row=3, column=0, sticky="w")
        self.copies = tk.StringVar(value="1")
        ttk.Spinbox(frm, from_=1, to=999, textvariable=self.copies, width=6).grid(
            row=3, column=1, sticky="w", pady=2)

        # apercu
        ttk.Label(frm, text="Apercu (1er article) :").grid(
            row=4, column=0, columnspan=2, sticky="w", pady=(8, 2))
        self.canvas = tk.Canvas(frm, width=340, height=180, bg="white",
                                highlightthickness=1, highlightbackground="#bbb")
        self.canvas.grid(row=5, column=0, columnspan=2)

        if not label_print.printing_available():
            ttk.Label(frm, foreground="#a11", wraplength=360, justify="left",
                      text="Impression directe indisponible sur ce poste "
                      "(necessite Windows + pywin32). L'apercu reste "
                      "disponible.").grid(row=6, column=0, columnspan=2,
                                          sticky="w", pady=(6, 0))

        btns = ttk.Frame(frm)
        btns.grid(row=7, column=0, columnspan=2, sticky="e", pady=(10, 0))
        ttk.Button(btns, text="Imprimer", command=self._print).pack(side="left", padx=4)
        ttk.Button(btns, text="Fermer", command=self.destroy).pack(side="left")

        self.transient(master)
        self.grab_set()
        self._draw_preview()

    def _current_model(self):
        return self.models[self.model_combo.current()]

    def _draw_preview(self):
        self.canvas.delete("all")
        model = self._current_model()
        item = self.items[0] if self.items else label_print.LabelItem(
            designation="Exemple", barcode="123456789", price=9.9)
        # echelle : faire tenir l'etiquette dans le canvas avec une marge
        cw, ch = 340, 180
        margin = 12
        scale = min((cw - 2 * margin) / model.width_mm,
                    (ch - 2 * margin) / model.height_mm)
        lw, lh = model.width_mm * scale, model.height_mm * scale
        ox, oy = (cw - lw) / 2, (ch - lh) / 2
        # cadre de l'etiquette
        self.canvas.create_rectangle(ox, oy, ox + lw, oy + lh,
                                     outline="#888", dash=(3, 2))
        r = label_print.TkCanvasRenderer(self.canvas, scale, ox=ox, oy=oy)
        try:
            label_print.layout_label(model, item, r)
        except Exception:                              # noqa: BLE001
            pass

    def _print(self):
        if not label_print.printing_available():
            messagebox.showerror(
                APP_TITLE, "Impression directe indisponible : ce poste doit "
                "etre sous Windows avec pywin32 (py -m pip install pywin32).",
                parent=self)
            return
        printer = self.printer_combo.get()
        if not printer or printer.startswith("("):
            messagebox.showwarning(APP_TITLE, "Choisissez une imprimante.", parent=self)
            return
        copies = parse_number(self.copies.get(), 1) or 1
        model = self._current_model()
        try:
            label_print.print_labels(printer, model, self.items, int(copies))
        except Exception as exc:                       # noqa: BLE001
            messagebox.showerror(APP_TITLE, "Echec d'impression :\n%s" % exc, parent=self)
            return
        self.printed = len(self.items) * int(copies)
        messagebox.showinfo(APP_TITLE, "%d etiquette(s) envoyee(s) a « %s »."
                            % (self.printed, printer), parent=self)
        self.destroy()


class ImportMappingDialog(tk.Toplevel):
    def __init__(self, master, columns):
        super().__init__(master)
        self.title("Correspondance des colonnes")
        self.result = None
        cols = ["(aucune)"] + list(columns)
        frm = ttk.Frame(self, padding=12)
        frm.pack(fill="both", expand=True)
        ttk.Label(frm, text="Indiquez quelle colonne du fichier correspond a chaque champ :"
                  ).grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 8))
        self.vars = {}
        rows = [("ref", "Reference (obligatoire)"), ("pv_ht", "Prix vente HT"),
                ("pv_ttc", "Prix vente TTC"),
                ("cb", "Codes equivalents (separes par ;)")]
        from editor_logic import norm
        for i, (key, label) in enumerate(rows, start=1):
            ttk.Label(frm, text=label).grid(row=i, column=0, sticky="w", pady=2)
            var = tk.StringVar()
            # pre-selection automatique par nom
            guess = "(aucune)"
            for c in columns:
                nc = norm(c)
                if key == "ref" and ("ref" in nc or "code article" in nc):
                    guess = c
                elif key == "pv_ht" and "ht" in nc and ("vente" in nc or "pv" in nc or "prix" in nc):
                    guess = c
                elif key == "pv_ttc" and "ttc" in nc:
                    guess = c
                elif key == "cb" and ("barre" in nc or "ean" in nc or "gencode" in nc):
                    guess = c
            var.set(guess)
            ttk.Combobox(frm, textvariable=var, values=cols, state="readonly",
                         width=30).grid(row=i, column=1, sticky="we", pady=2)
            self.vars[key] = var
        btns = ttk.Frame(frm)
        btns.grid(row=len(rows) + 1, column=0, columnspan=2, sticky="e", pady=(10, 0))
        ttk.Button(btns, text="Importer", command=self._ok).pack(side="left", padx=4)
        ttk.Button(btns, text="Annuler", command=self.destroy).pack(side="left")
        self.transient(master)
        self.grab_set()

    def _ok(self):
        res = {k: (v.get() if v.get() != "(aucune)" else None) for k, v in self.vars.items()}
        if not res.get("ref"):
            messagebox.showwarning(APP_TITLE, "La colonne 'Reference' est obligatoire.", parent=self)
            return
        self.result = res
        self.destroy()


# --------------------------------------------------------------------------- #
#  Lecture tabulaire Excel/CSV -> liste de dicts
# --------------------------------------------------------------------------- #
def _read_tabular(path):
    ext = os.path.splitext(path)[1].lower()
    if ext in (".xlsx", ".xls"):
        import openpyxl
        wb = openpyxl.load_workbook(path, data_only=True)
        ws = wb.active
        rows = list(ws.iter_rows(values_only=True))
        if not rows:
            return []
        header = [str(c).strip() if c is not None else "" for c in rows[0]]
        out = []
        for r in rows[1:]:
            out.append({header[i]: r[i] for i in range(len(header))})
        return out
    # CSV
    with open(path, "r", encoding="utf-8-sig", newline="") as fh:
        sample = fh.read(2048)
        fh.seek(0)
        delim = ";" if sample.count(";") >= sample.count(",") else ","
        return list(csv.DictReader(fh, delimiter=delim))


# --------------------------------------------------------------------------- #
#  Point d'entree
# --------------------------------------------------------------------------- #
def open_repository(root, args):
    """Etablit la connexion (ou le mode demo) et renvoie un repository pret."""
    if args.demo:
        return DemoRepository().connect()

    cfg_path = args.config or _default_config_path()
    try:
        cfg = load_config(cfg_path)
        config_ok = True
    except DBError as exc:
        # config.json illisible : on previent et on ouvre la fenetre de connexion
        messagebox.showerror(APP_TITLE, str(exc))
        cfg = dict(article_db.DEFAULT_CONFIG)
        config_ok = False
    if args.db:
        cfg["database"] = args.db

    # Connexion directe et silencieuse si une config valide existe deja
    # (et qu'on ne force pas le dialogue). Sinon : ecran de connexion.
    if config_ok and os.path.isfile(cfg_path) and not args.ask:
        try:
            return ArticleRepository(cfg).connect()
        except DBError:
            pass   # on bascule sur l'ecran de connexion ci-dessous

    repo, cfg = prompt_connection(root, cfg)
    if repo is not None:
        try:
            save_config(cfg_path, cfg)     # memorise les parametres qui marchent
        except OSError:
            pass
    return repo


def prompt_connection(root, cfg, allow_demo=True):
    """Affiche l'ecran de connexion en boucle. Renvoie (repo, cfg).

    repo est deja connecte (ArticleRepository ou DemoRepository), ou None si
    l'utilisateur annule.
    """
    dlg = ConnectDialog(root, cfg, allow_demo=allow_demo)
    root.wait_window(dlg)
    if dlg.demo:
        return DemoRepository().connect(), cfg
    if dlg.repo is not None:
        return dlg.repo, dlg.result
    return None, cfg


def _default_config_path():
    """Chemin de config.json, A COTE de l'exe une fois empaquete (PyInstaller).

    En .exe --onefile, __file__ pointe vers un dossier temporaire (_MEIPASS)
    efface a la fermeture : on prend alors le dossier de l'executable pour que
    config.json soit persistant et editable a cote du programme."""
    if getattr(sys, "frozen", False):
        base = os.path.dirname(os.path.abspath(sys.executable))
    else:
        base = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(base, "config.json")


def main(argv=None):
    ap = argparse.ArgumentParser(description="Editeur en masse des articles PRIME.")
    ap.add_argument("--config", help="Fichier de configuration JSON (connexion).")
    ap.add_argument("--db", help="Chemin du .FDB (surcharge la config).")
    ap.add_argument("--ask", action="store_true",
                    help="Toujours afficher la boite de connexion au demarrage.")
    ap.add_argument("--demo", action="store_true",
                    help="Mode demonstration : donnees en memoire, sans Firebird.")
    args = ap.parse_args(argv)

    root = tk.Tk()
    root.title(APP_TITLE)
    root.geometry("1120x680")
    try:
        ttk.Style().theme_use("clam")
    except tk.TclError:
        pass

    repo = open_repository(root, args)
    if repo is None:
        root.destroy()
        return 0

    root._app = BulkEditorApp(root, repo, config_path=args.config)
    root.protocol("WM_DELETE_WINDOW", lambda: _on_close(root))
    root.mainloop()
    return 0


def _on_close(root):
    app = getattr(root, "_app", None)
    if app is not None and app._has_pending() and \
            not messagebox.askyesno(
                APP_TITLE,
                "Des modifications ne sont pas enregistrees. Quitter quand meme ?"):
        return
    try:
        if app is not None:
            app.repo.close()
    finally:
        root.destroy()


if __name__ == "__main__":
    sys.exit(main())
