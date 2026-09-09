#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Interface graphique pour import_bon_reception.py
================================================

GUI de bureau (PySide6) qui pilote l'outil `import_bon_reception.py`.

  1. choisir le fichier Excel du fournisseur,
  2. choisir la base Firebird (.FDB) + fournisseur + depot,
  3. APERCU (--dry-run) qui n'ecrit rien,
  4. IMPORT reel avec resume.

Conception
----------
La GUI ne reimplemente AUCUNE logique d'import :
  * l'apercu des lignes utilise `read_excel` de l'outil (hors-ligne, sans DB) ;
  * l'execution relance le programme lui-meme en mode "--run-cli", qui appelle
    le `main()` de l'outil d'origine. Le comportement est donc identique a la
    ligne de commande, et ce mecanisme fonctionne aussi bien en script qu'une
    fois empaquete en .exe (PyInstaller --onefile).

Lancement
---------
    pip install PySide6 openpyxl fdb
    python import_bon_reception_gui.py

Empaquetage .exe (sur Windows) :
    pyinstaller --onefile --windowed --name ImportBonReception \\
        --add-data "import_bon_reception.py;." import_bon_reception_gui.py
"""

import contextlib
import importlib.util
import json
import os
import re
import sys
import tempfile
import traceback

SCRIPT_NAME = "import_bon_reception.py"
CLEAN_SCRIPT_NAME = "nettoyer_articles.py"
REPAIR_SCRIPT_NAME = "reparer_encodage.py"
CHARSETS = ["WIN1256", "WIN1252", "ISO8859_1", "UTF8", "NONE", "DOS850"]
ORG, APP = "PrimeOffice", "ImportBonReception"

try:
    GUI_SCRIPT = os.path.abspath(__file__)
except NameError:  # pragma: no cover
    GUI_SCRIPT = os.path.abspath(sys.argv[0])

DEFAULT_ARRONDI = [[200, 5], [1000, 10], [None, 50]]


def parse_arrondi(text):
    """\"200:5, 1000:10, *:50\"  ->  [[200,5],[1000,10],[None,50]]"""
    tiers = []
    for part in (text or "").split(","):
        part = part.strip()
        if not part or ":" not in part:
            continue
        seuil, pas = (x.strip() for x in part.split(":", 1))
        try:
            s = None if seuil in ("*", "", "null", "none") else int(float(seuil))
            p = float(pas)
            p = int(p) if p == int(p) else p
        except ValueError:
            continue
        tiers.append([s, p])
    return tiers or [list(t) for t in DEFAULT_ARRONDI]


def format_arrondi(tiers):
    """[[200,5],[1000,10],[None,50]]  ->  \"200:5, 1000:10, *:50\""""
    if isinstance(tiers, str):
        return tiers
    parts = []
    for t in tiers or []:
        try:
            seuil, pas = t[0], t[1]
        except (IndexError, TypeError):
            continue
        s = "*" if seuil is None else str(seuil)
        p = str(int(pas)) if float(pas) == int(pas) else str(pas)
        parts.append("%s:%s" % (s, p))
    return ", ".join(parts)


# --------------------------------------------------------------------------- #
#  Localisation / chargement de l'outil d'origine
# --------------------------------------------------------------------------- #
def app_dir():
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(GUI_SCRIPT)


def resolve_named_script(name):
    """Trouve un script (import_bon_reception.py / nettoyer_articles.py) :
    dans le bundle PyInstaller (_MEIPASS) si gele, sinon a cote de ce fichier."""
    candidates = []
    if getattr(sys, "frozen", False):
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            candidates.append(os.path.join(meipass, name))
    candidates.append(os.path.join(app_dir(), name))
    for c in candidates:
        if os.path.isfile(c):
            return c
    return None


def resolve_script_path():
    return resolve_named_script(SCRIPT_NAME)


def load_tool_module(script_path):
    """Importe l'outil par chemin. Renvoie (module, erreur). L'outil fait
    `import fdb` / `import openpyxl` au chargement (sys.exit si absent) :
    on intercepte SystemExit pour ne pas tuer l'appelant."""
    if not script_path or not os.path.isfile(script_path):
        return None, "Script %s introuvable." % SCRIPT_NAME
    try:
        spec = importlib.util.spec_from_file_location("import_bon_reception", script_path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod, None
    except SystemExit as exc:
        return None, str(exc.code or "Dependance manquante (openpyxl/fdb).")
    except Exception as exc:  # noqa: BLE001
        return None, "%s: %s" % (type(exc).__name__, exc)


def friendly_error(raw):
    """Traduit les pannes courantes en francais lisible pour un debutant."""
    t = (raw or "").lower()
    # IMPORTANT : l'ordre compte. Les pannes RESEAU (hote/serveur) doivent etre
    # testees AVANT les identifiants : Firebird signale beaucoup d'erreurs
    # (reseau compris) avec le meme code generique -902, qu'il ne faut donc PAS
    # interpreter comme « mot de passe incorrect ».
    rules = [
        (("module manquant : fdb", "no module named 'fdb'", "import fdb"),
         "Le pilote Firebird « fdb » n'est pas installe sur ce poste "
         "(pip install fdb)."),
        (("module manquant : openpyxl", "no module named 'openpyxl'"),
         "Le module « openpyxl » (lecture Excel) n'est pas installe "
         "(pip install openpyxl)."),
        (("fbclient", "client library", "load_api", "libfbclient"),
         "Librairie cliente Firebird introuvable (fbclient.dll). Indiquez son "
         "chemin dans les options avancees."),
        # --- Reseau / serveur injoignable (teste AVANT les identifiants) ---
        (("unable to complete network request", "network request to host",
          "failed to establish a connection", "connection refused",
          "connection rejected", "unavailable database", "host unknown",
          "connection lost", "is not available", "network error",
          "winsock", "10060", "10061", "getaddrinfo"),
         "Serveur Firebird injoignable : verifiez l'hote et le port (3050), que "
         "le serveur Firebird tourne sur le poste serveur, et que le pare-feu "
         "autorise le port. Pour un acces local, laissez l'hote VIDE."),
        # --- Version/protocole client <-> serveur (frequent en reseau FB2.5) ---
        (("incompatible wire encryption", "missing security context",
          "error occurred during login", "srp", "authentication, client",
          "client plugin requires", "no matching authentication"),
         "Le client et le serveur Firebird ne sont pas compatibles (version ou "
         "methode d'authentification). Utilisez un fbclient.dll de la MEME "
         "version que le serveur (Firebird 2.5) — voir « Librairie cliente FB » "
         "dans les options."),
        # --- Identifiants : seulement des messages SPECIFIQUES ---
        (("are not defined", "user name and password",
          "incorrect user name or password", "invalid username or password",
          "password incorrect", "incorrect user"),
         "Identifiant ou mot de passe incorrect.\n"
         "Note : en acces LOCAL (hote vide), Firebird embedded ne verifie PAS "
         "le mot de passe ; en RESEAU, le serveur exige le vrai mot de passe "
         "SYSDBA du poste serveur (pas forcement « masterkey »)."),
        (("i/o error", "no such file", "cannot open", "unable to open",
          "error while trying to open file"),
         "Base de donnees introuvable : verifiez le chemin du .FDB. En reseau, "
         "le chemin doit etre celui vu PAR LE SERVEUR (ex. C:\\PRIME\\PR22.FDB) "
         "ou un alias Firebird."),
        (("malformed string", "transliteration", "charset"),
         "Probleme d'encodage : pour l'arabe choisissez le charset WIN1256."),
        (("excel introuvable", "fichier excel"),
         "Fichier Excel introuvable."),
        (("ligne d'en-tete", "ref. art", "colonne obligatoire", "colonne de prix"),
         "Format Excel non reconnu : il faut une colonne « Ref. Art. », une "
         "quantite et un prix."),
    ]
    for keys, msg in rules:
        if any(k in t for k in keys):
            return msg
    s = (raw or "").strip()
    if not s:
        return "Erreur inconnue."
    return s.splitlines()[-1][:300]


# --------------------------------------------------------------------------- #
#  Mode "--run-cli" : lance le main() de l'outil dans ce meme binaire
# --------------------------------------------------------------------------- #
def run_cli(argv):
    """Execute import_bon_reception.main() avec argv. Utilise par la GUI via
    un sous-processus (fonctionne en script ET en .exe gele)."""
    script = resolve_script_path()
    mod, err = load_tool_module(script)
    if not mod:
        print(err)
        return 1
    sys.argv = ["import_bon_reception"] + list(argv)
    try:
        mod.main()
        return 0
    except SystemExit as exc:
        code = exc.code
        if isinstance(code, str):
            print(code)
            return 1
        return int(code) if code else 0
    except Exception:  # noqa: BLE001
        traceback.print_exc()
        return 1


def _run_helper_script(script_name, mod_name, argv):
    """Charge un script auxiliaire (nettoyer/reparer) en lui rendant
    'import_bon_reception' importable, puis appelle son main(argv).
    Fonctionne en script ET en .exe gele."""
    path = resolve_named_script(script_name)
    if not path:
        print("Script %s introuvable." % script_name)
        return 1
    tool, err = load_tool_module(resolve_script_path())
    if not tool:
        print(err)
        return 1
    sys.modules["import_bon_reception"] = tool
    try:
        spec = importlib.util.spec_from_file_location(mod_name, path)
        helper = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(helper)
    except SystemExit as exc:
        print(str(exc.code or "Dependance manquante."))
        return 1
    except Exception:  # noqa: BLE001
        traceback.print_exc()
        return 1
    sys.argv = [mod_name] + list(argv)
    try:
        helper.main()
        return 0
    except SystemExit as exc:
        code = exc.code
        if isinstance(code, str):
            print(code)
            return 1
        return int(code) if code else 0
    except Exception:  # noqa: BLE001
        traceback.print_exc()
        return 1


def run_clean(argv):
    return _run_helper_script(CLEAN_SCRIPT_NAME, "nettoyer_articles", argv)


def run_repair(argv):
    return _run_helper_script(REPAIR_SCRIPT_NAME, "reparer_encodage", argv)


# --------------------------------------------------------------------------- #
#  Delegate combobox pour la colonne Famille de la table
# --------------------------------------------------------------------------- #
def _make_famille_delegate_class():
    from PySide6.QtWidgets import QStyledItemDelegate, QComboBox
    from PySide6.QtCore import QModelIndex

    class FamilleDelegate(QStyledItemDelegate):
        """Affiche un QComboBox dans la cellule Famille de la table."""
        def __init__(self, familles=None, parent=None):
            super().__init__(parent)
            self._familles = familles or []  # liste de {"code":..., "intitule":...}

        def set_familles(self, familles):
            self._familles = familles

        def createEditor(self, parent, option, index):
            cb = QComboBox(parent)
            cb.setEditable(True)
            for f in self._familles:
                cb.addItem(f["intitule"], f["code"])
            return cb

        def setEditorData(self, editor, index):
            val = index.data()
            idx = editor.findText(str(val or ""))
            if idx >= 0:
                editor.setCurrentIndex(idx)
            else:
                editor.setEditText(str(val or ""))

        def setModelData(self, editor, model, index):
            model.setData(index, editor.currentText())

    return FamilleDelegate


# --------------------------------------------------------------------------- #
#  GUI
# --------------------------------------------------------------------------- #
def _window_class():
    from PySide6.QtCore import Qt, QProcess, QSettings
    from PySide6.QtGui import QFont, QTextCursor, QColor
    from PySide6.QtWidgets import (
        QApplication, QWidget, QLabel, QLineEdit, QPushButton, QFileDialog,
        QCheckBox, QSpinBox, QDoubleSpinBox, QComboBox, QPlainTextEdit,
        QTableWidget, QTableWidgetItem, QGroupBox, QFormLayout, QGridLayout,
        QVBoxLayout, QHBoxLayout, QMessageBox, QTabWidget, QSizePolicy,
        QHeaderView, QAbstractItemView, QProgressBar, QMenu,
    )

    FamilleDelegate = _make_famille_delegate_class()

    # Couleurs pour le statut des lignes
    COLOR_EXACT   = QColor(0xd4, 0xed, 0xda)   # vert clair
    COLOR_MATCHED = QColor(0xff, 0xf3, 0xcd)   # jaune clair
    COLOR_NEW     = QColor(0xf8, 0xd7, 0xda)   # rose / rouge clair
    COLOR_WARN    = QColor(0xff, 0xc1, 0x07)   # orange (OCR anomalie)

    class MainWindow(QWidget):
        # --- indices des colonnes de la table ---
        COL_REF     = 0
        COL_DESIG   = 1
        COL_QTE     = 2
        COL_PA      = 3   # prix achat HT
        COL_PV      = 4   # prix vente HT
        COL_TVA     = 5
        COL_FAM     = 6
        COL_CB      = 7   # code-barres
        COL_MAJ_PA  = 8   # MAJ prix achat ? (checkbox)
        COL_STATUT  = 9   # lecture seule
        COL_MATCH   = 10  # article existant (match) — lecture seule
        COL_TOTAL   = 11  # lecture seule

        FIELD_KEYS = ("database", "code_tiers", "raison_sociale", "code_depot",
                      "host", "port", "user", "charset", "fb_client_library",
                      "code_type_piece", "default_famille", "default_famille_intitule",
                      "default_tva", "default_unite", "default_unite_intitule",
                      "colonne_prix", "reserved_id_threshold",
                      "refdoc", "maj_prix_achat_seuil_pct")

        def __init__(self):
            super().__init__()
            self.setWindowTitle("Import Bon de reception — Prime Office")
            self.resize(1200, 850)

            self.script_path = resolve_script_path()
            self.tool_mod, self.tool_err = load_tool_module(self.script_path)
            self.proc = None
            self.tmp_config_path = None
            self.tmp_lines_path = None
            self.current_mode = None
            self._last_nopiece = None       # pour "Annuler le dernier import"

            # Suivi des lignes dont PV a ete saisie manuellement (index de ligne)
            self._pv_manual = set()
            # Rapprochements appliques : row -> {"orig":ref fournisseur, "match":ref trouvee}
            # pour pouvoir REFUSER un mauvais rapprochement et recreer l'article.
            self._match_rows = {}
            # Garde contre la recursion dans cellChanged
            self._cc_guard = False

            # Cache des familles chargees depuis la base
            self._familles_list = []

            self._create_fields()
            self._build_ui()
            self._apply_defaults_from_tool()
            self._load_settings()
            self._refresh_script_banner()

            # Repondu des signaux apres construction complete
            # (PV/arrondi/marge -> recomputer toutes les PV, PAS recharger l'Excel)
            self.f_prix_vente_auto.toggled.connect(self._recompute_all_pv)
            self.f_marge.valueChanged.connect(self._recompute_all_pv)
            self.f_arrondi.editingFinished.connect(self._recompute_all_pv)
            self.f_charset.currentTextChanged.connect(lambda *_: self._reload_preview())
            self.f_ref_designation.toggled.connect(lambda *_: self._reload_preview())

        # ---- creation des widgets de saisie (noms stables) -------------- #
        def _create_fields(self):
            self.excel_edit = QLineEdit(readOnly=True)
            self.excel_edit.setPlaceholderText("Fichier .xlsx ou .pdf du fournisseur…")

            self.f_database = QLineEdit()
            self.f_database.setPlaceholderText("C:\\PRIME\\PR22.FDB")

            # Fournisseur / depot : QComboBox editable (rempli par "Charger les listes")
            self.f_code_tiers = QComboBox(); self.f_code_tiers.setEditable(True)
            self.f_code_tiers.setPlaceholderText("ex. F001 — vide = aucun")
            self.f_raison = QLineEdit()
            self.f_raison.setPlaceholderText("Nom du fournisseur (si creation)")
            self.f_code_depot = QComboBox(); self.f_code_depot.setEditable(True)
            self.f_code_depot.setPlaceholderText("Code depot — vide = aucun")

            self.f_host = QLineEdit()
            self.f_host.setPlaceholderText("localhost — vide = acces local au fichier")
            self.f_port = QSpinBox(); self.f_port.setRange(0, 65535); self.f_port.setValue(3050)
            self.f_user = QLineEdit()
            self.f_password = QLineEdit(); self.f_password.setEchoMode(QLineEdit.EchoMode.Password)
            self.f_charset = QComboBox(); self.f_charset.addItems(CHARSETS); self.f_charset.setEditable(True)
            self.f_fbclient = QLineEdit()
            self.f_fbclient.setPlaceholderText("Chemin de fbclient.dll (optionnel)")

            self.f_code_type_piece = QLineEdit("PC_AC_B")
            self.f_date = QLineEdit(); self.f_date.setPlaceholderText("AAAA-MM-JJ — vide = aujourd'hui")
            self.f_refdoc = QLineEdit()
            self.f_refdoc.setPlaceholderText("N° du bon fournisseur (ex: BL-2024-001)")
            self.f_create_tiers = QCheckBox("Creer le fournisseur / depot s'ils manquent")
            self.f_create_tiers.setChecked(True)

            # Famille par defaut : QComboBox editable (rempli par "Charger les listes")
            self.f_default_famille = QComboBox(); self.f_default_famille.setEditable(True)
            self.f_default_famille.addItem("TOUS")
            self.f_default_famille_int = QLineEdit("Tous")
            self.f_match_famille = QCheckBox("Rapprocher la colonne « Famille » d'une famille existante (par nom)")
            self.f_match_famille.setChecked(True)
            self.f_create_familles = QCheckBox("Creer la famille (par son nom) si aucune ne correspond")
            self.f_create_familles.setChecked(False)
            self.f_default_tva = QDoubleSpinBox(); self.f_default_tva.setRange(0, 100)
            self.f_default_tva.setDecimals(2); self.f_default_tva.setValue(19); self.f_default_tva.setSuffix(" %")
            self.f_calc_ttc = QCheckBox("Renseigner aussi le prix d'achat TTC")
            self.f_calc_ttc.setChecked(True)
            self.f_colonne_prix = QComboBox(); self.f_colonne_prix.addItems(["prix"]); self.f_colonne_prix.setEditable(True)
            self.f_default_unite = QLineEdit(); self.f_default_unite.setPlaceholderText("Vide = aucune unite")
            self.f_default_unite_int = QLineEdit("Unite")
            self.f_barcode_ref = QCheckBox("Recopier la reference dans CODE_BARRES")
            self.f_maj_code_barres = QCheckBox(
                "Importer les codes-barres (visibles dans la fiche article), y compris sur les articles existants")
            self.f_maj_code_barres.setChecked(True)
            self.f_reserved = QSpinBox(); self.f_reserved.setRange(0, 2_000_000_000); self.f_reserved.setValue(1_000_000)
            self.f_maj_seuil = QDoubleSpinBox(); self.f_maj_seuil.setRange(0, 1000)
            self.f_maj_seuil.setDecimals(0); self.f_maj_seuil.setValue(40); self.f_maj_seuil.setSuffix(" %")
            self.f_match_par_desig = QCheckBox("Retrouver les articles existants par le numero dans la designation")
            self.f_match_par_desig.setChecked(True)
            self.f_ref_designation = QCheckBox(
                "Ajouter la reference a la designation si absente (sauf si c'est un code-barres)")
            self.f_ref_designation.setChecked(True)

            # --- prix de vente automatique ---
            self.f_prix_vente_auto = QCheckBox("Calculer un prix de vente automatique (nouveaux articles)")
            self.f_prix_vente_auto.setChecked(True)
            self.f_marge = QDoubleSpinBox(); self.f_marge.setRange(0, 1000); self.f_marge.setDecimals(2)
            self.f_marge.setValue(50); self.f_marge.setSuffix(" %")
            self.f_arrondi = QLineEdit("200:5, 1000:10, *:50")
            self.f_arrondi.setPlaceholderText("seuil:pas, ... (ex. 200:5, 1000:10, *:50 ; * = au-dela)")

        # ---- assemblage ------------------------------------------------- #
        def _build_ui(self):
            root = QVBoxLayout(self)
            root.setContentsMargins(14, 12, 14, 12); root.setSpacing(10)

            title = QLabel("Import d'un Bon de reception depuis un Excel fournisseur")
            tf = QFont(); tf.setPointSize(14); tf.setBold(True); title.setFont(tf)
            root.addWidget(title)

            self.script_banner = QLabel(wordWrap=True)
            self.script_banner.setTextFormat(Qt.TextFormat.RichText)
            self.script_banner.linkActivated.connect(self.locate_script)
            root.addWidget(self.script_banner)

            # --- panneau ESSENTIEL ---
            ess = QGroupBox("L'essentiel")
            g = QGridLayout(ess); g.setColumnStretch(1, 1)

            g.addWidget(QLabel("<b>Fichier (Excel/PDF)</b>"), 0, 0, Qt.AlignmentFlag.AlignRight)
            g.addWidget(self.excel_edit, 0, 1)
            b1 = QPushButton("Parcourir…"); b1.clicked.connect(self.pick_excel)
            g.addWidget(b1, 0, 2)

            g.addWidget(QLabel("<b>Base Firebird (.FDB)</b>"), 1, 0, Qt.AlignmentFlag.AlignRight)
            g.addWidget(self.f_database, 1, 1)
            b2 = QPushButton("Parcourir…"); b2.clicked.connect(self.pick_fdb)
            g.addWidget(b2, 1, 2)

            test_row = QHBoxLayout()
            self.btn_test = QPushButton("Tester la connexion"); self.btn_test.clicked.connect(self.test_connection)
            self.btn_load_lists = QPushButton("Charger les listes"); self.btn_load_lists.clicked.connect(self.load_lists)
            self.conn_result = QLabel("")
            self.conn_result.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            test_row.addWidget(self.btn_test); test_row.addWidget(self.btn_load_lists)
            test_row.addWidget(self.conn_result, 1)
            tw = QWidget(); tw.setLayout(test_row)
            g.addWidget(tw, 2, 1, 1, 2)

            g.addWidget(QLabel("Fournisseur"), 3, 0, Qt.AlignmentFlag.AlignRight)
            sup = QHBoxLayout()
            sup.addWidget(QLabel("code")); sup.addWidget(self.f_code_tiers)
            sup.addWidget(QLabel("nom")); sup.addWidget(self.f_raison, 1)
            sw = QWidget(); sw.setLayout(sup)
            g.addWidget(sw, 3, 1, 1, 2)

            g.addWidget(QLabel("Depot"), 4, 0, Qt.AlignmentFlag.AlignRight)
            g.addWidget(self.f_code_depot, 4, 1, 1, 2)

            g.addWidget(QLabel("N° bon fournisseur"), 5, 0, Qt.AlignmentFlag.AlignRight)
            g.addWidget(self.f_refdoc, 5, 1, 1, 2)
            root.addWidget(ess)

            # --- bouton options avancees ---
            self.adv_btn = QPushButton("▸ Options avancees")
            self.adv_btn.setCheckable(True)
            self.adv_btn.setStyleSheet("QPushButton { text-align:left; border:none; color:#235; }")
            self.adv_btn.toggled.connect(self._toggle_advanced)
            root.addWidget(self.adv_btn)

            self.adv = QTabWidget(); self.adv.setVisible(False)
            self.adv.addTab(self._tab_connexion(), "Connexion")
            self.adv.addTab(self._tab_bon(), "Bon de reception")
            self.adv.addTab(self._tab_articles(), "Familles & articles")
            root.addWidget(self.adv)

            # --- actions ---
            actions = QHBoxLayout()
            self.btn_preview = QPushButton("Apercu (dry-run)")
            self.btn_preview.setToolTip("Simulation : n'ecrit rien (rollback).")
            self.btn_preview.clicked.connect(lambda: self.run_import(True))
            self.btn_import = QPushButton("Importer reellement")
            self.btn_import.clicked.connect(lambda: self.run_import(False))
            bf = QFont(); bf.setBold(True)
            for b in (self.btn_preview, self.btn_import):
                b.setFont(bf); b.setMinimumHeight(38)
            self.btn_import.setStyleSheet("QPushButton { background:#7a1f1f; color:white; }")

            self.btn_match = QPushButton("Rechercher correspondances")
            self.btn_match.setToolTip("Rapprocher chaque ligne a un article existant (par designation)")
            self.btn_match.clicked.connect(self.run_match_action)

            self.btn_undo = QPushButton("Annuler le dernier import")
            self.btn_undo.setToolTip("Annule le dernier bon cree (ANNULEE=0, stock repris)")
            self.btn_undo.setEnabled(False)
            self.btn_undo.clicked.connect(self.cancel_last_import)

            self.btn_template = QPushButton("Creer un modele Excel")
            self.btn_template.clicked.connect(self.make_template_action)

            self.btn_sync_bc = QPushButton("Synchroniser codes-barres")
            self.btn_sync_bc.setToolTip(
                "Reparer les articles DEJA importes : rend leurs codes-barres "
                "visibles dans la fiche article (copie vers EQUIV_CBARRES).")
            self.btn_sync_bc.clicked.connect(self.run_sync_barcodes_action)

            self.btn_apply_bc = QPushButton("Appliquer codes-barres (sans stock)")
            self.btn_apply_bc.setToolTip(
                "Reprend les codes-barres du fichier/table charge et les applique "
                "aux articles EXISTANTS (sans creer de bon ni toucher au stock). "
                "Ideal pour corriger d'anciens imports : rechargez le meme Excel "
                "fournisseur puis cliquez ici.")
            self.btn_apply_bc.clicked.connect(self.run_apply_barcodes_action)

            self.btn_repair = QPushButton("Reparer l'arabe")
            self.btn_repair.setToolTip(
                "Corriger sur place les noms arabes deformes par un import en UTF8 "
                "(re-encodage en WIN1256, sans toucher aux prix ni au stock).")
            self.btn_repair.clicked.connect(self.run_repair_action)
            self.btn_clean = QPushButton("Nettoyer « ? »")
            self.btn_clean.setToolTip(
                "Supprimer les articles corrompus (designation contenant « ? ») "
                "issus d'un import au mauvais charset.")
            self.btn_clean.clicked.connect(self.run_clean_action)
            self.btn_cancel = QPushButton("Arreter"); self.btn_cancel.setEnabled(False)
            self.btn_cancel.clicked.connect(self.cancel_run)

            actions.addWidget(self.btn_preview); actions.addWidget(self.btn_import)
            actions.addWidget(self.btn_match)
            actions.addStretch(1)
            actions.addWidget(self.btn_undo)
            actions.addWidget(self.btn_template)
            actions.addWidget(self.btn_sync_bc)
            actions.addWidget(self.btn_apply_bc)
            actions.addWidget(self.btn_repair); actions.addWidget(self.btn_clean)
            actions.addWidget(self.btn_cancel)
            root.addLayout(actions)

            self.busy = QProgressBar(); self.busy.setRange(0, 1); self.busy.setValue(0)
            self.busy.setTextVisible(False); self.busy.setFixedHeight(6)
            root.addWidget(self.busy)

            # --- sorties ---
            out = QTabWidget()

            # -- onglet Apercu des lignes (table editable) --
            pv = QWidget(); pvl = QVBoxLayout(pv); pvl.setContentsMargins(0, 0, 0, 0)
            self.preview_count = QLabel("Aucun fichier charge.")
            pvl.addWidget(self.preview_count)

            self.preview_table = QTableWidget(0, 12)
            self.preview_table.setHorizontalHeaderLabels([
                "Ref. Art.", "Designation", "Qte", "Prix achat HT", "Prix vente",
                "TVA %", "Famille", "Code-barres", "MAJ PA?",
                "Statut", "Article existant (match)", "Total HT"])
            # Declencheurs d'edition : double-clic ou touche d'edition
            self.preview_table.setEditTriggers(
                QAbstractItemView.EditTrigger.DoubleClicked |
                QAbstractItemView.EditTrigger.EditKeyPressed)
            self.preview_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
            self.preview_table.horizontalHeader().setSectionResizeMode(
                self.COL_DESIG, QHeaderView.ResizeMode.Stretch)
            self.preview_table.horizontalHeader().setSectionResizeMode(
                self.COL_MATCH, QHeaderView.ResizeMode.ResizeToContents)
            self._famille_delegate = FamilleDelegate(parent=self.preview_table)
            self.preview_table.setItemDelegateForColumn(self.COL_FAM, self._famille_delegate)
            self.preview_table.cellChanged.connect(self._on_cell_changed)
            # Clic droit sur une ligne : refuser / accepter un rapprochement.
            self.preview_table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
            self.preview_table.customContextMenuRequested.connect(self._show_table_menu)

            pvl.addWidget(self.preview_table, 1)
            out.addTab(pv, "Apercu des lignes")

            out.addTab(self._build_summary_widget(), "Resume")

            self.log = QPlainTextEdit(readOnly=True)
            self.log.setFont(QFont("Menlo, Consolas, monospace"))
            out.addTab(self.log, "Journal")
            out.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
            root.addWidget(out, 1)

            self.status = QLabel("Pret."); self.status.setStyleSheet("color:#555;")
            root.addWidget(self.status)

        def _toggle_advanced(self, on):
            self.adv.setVisible(on)
            self.adv_btn.setText(("▾ " if on else "▸ ") + "Options avancees")

        def _tab_connexion(self):
            w = QWidget(); f = QFormLayout(w)
            f.addRow("Hote", self.f_host)
            f.addRow("Port", self.f_port)
            f.addRow("Utilisateur", self.f_user)
            f.addRow("Mot de passe", self.f_password)
            f.addRow("Charset", self.f_charset)
            rc = QHBoxLayout(); rc.addWidget(self.f_fbclient, 1)
            bb = QPushButton("…"); bb.setFixedWidth(34); bb.clicked.connect(self.pick_fbclient)
            rc.addWidget(bb); rcw = QWidget(); rcw.setLayout(rc)
            f.addRow("Librairie cliente FB", rcw)
            return w

        def _tab_bon(self):
            w = QWidget(); f = QFormLayout(w)
            f.addRow("Type de piece", self.f_code_type_piece)
            f.addRow("Date du bon", self.f_date)
            f.addRow("", self.f_create_tiers)
            return w

        def _tab_articles(self):
            w = QWidget(); f = QFormLayout(w)
            f.addRow("Famille par defaut — code", self.f_default_famille)
            f.addRow("Famille par defaut — nom", self.f_default_famille_int)
            f.addRow("", self.f_match_famille)
            f.addRow("", self.f_create_familles)
            f.addRow("TVA par defaut", self.f_default_tva)
            f.addRow("", self.f_calc_ttc)
            f.addRow("Colonne prix", self.f_colonne_prix)
            f.addRow("Unite par defaut — code", self.f_default_unite)
            f.addRow("Unite par defaut — nom", self.f_default_unite_int)
            f.addRow("", self.f_barcode_ref)
            f.addRow("", self.f_maj_code_barres)
            f.addRow("Seuil ID reserves", self.f_reserved)
            f.addRow(QLabel("<b>Prix de vente</b>"))
            f.addRow("", self.f_prix_vente_auto)
            f.addRow("Marge", self.f_marge)
            f.addRow("Arrondi (seuil:pas)", self.f_arrondi)
            f.addRow(QLabel("<b>Rapprochement</b>"))
            f.addRow("", self.f_match_par_desig)
            f.addRow("", self.f_ref_designation)
            f.addRow("Seuil alerte MAJ prix achat", self.f_maj_seuil)
            return w

        def _build_summary_widget(self):
            w = QWidget(); grid = QGridLayout(w); grid.setColumnStretch(1, 1)
            self.summary_state = QLabel("—")
            sf = QFont(); sf.setPointSize(13); sf.setBold(True); self.summary_state.setFont(sf)
            self.summary_state.setWordWrap(True)
            grid.addWidget(self.summary_state, 0, 0, 1, 2)
            self.sum_fields = {}
            rows = [("piece", "Bon de reception"), ("tiers", "Fournisseur / depot"),
                    ("lus", "Lignes lues"), ("crees", "Articles crees"),
                    ("existants", "Articles existants"), ("maj", "Prix achat mis a jour"),
                    ("barcodes", "Codes-barres renseignes"),
                    ("ht", "Total HT"), ("tva", "Total TVA"), ("ttc", "Total TTC")]
            for i, (k, lab) in enumerate(rows, start=1):
                grid.addWidget(QLabel("<b>%s</b>" % lab), i, 0,
                               Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignTop)
                v = QLabel("—"); v.setWordWrap(True)
                v.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
                self.sum_fields[k] = v; grid.addWidget(v, i, 1)
            grid.setRowStretch(len(rows) + 1, 1)
            return w

        # ---- defaults / settings --------------------------------------- #
        def _apply_defaults_from_tool(self):
            if not self.tool_mod:
                return
            dc = getattr(self.tool_mod, "DEFAULT_CONFIG", {})
            if dc.get("database"):
                self.f_database.setText(str(dc["database"]).replace("\\\\", "\\"))
            self.f_user.setText(dc.get("user", "SYSDBA"))
            self.f_password.setText(dc.get("password", "masterkey"))
            cs = dc.get("charset", "WIN1256")
            if cs and self.f_charset.findText(cs) < 0:
                self.f_charset.insertItem(0, cs)
            self.f_charset.setCurrentText(cs)
            if "prix_vente_auto" in dc:
                self.f_prix_vente_auto.setChecked(bool(dc["prix_vente_auto"]))
            if "marge_pct" in dc:
                try: self.f_marge.setValue(float(dc["marge_pct"]))
                except (TypeError, ValueError): pass
            if dc.get("arrondi_paliers"):
                self.f_arrondi.setText(format_arrondi(dc["arrondi_paliers"]))

        def _settings(self):
            return QSettings(ORG, APP)

        def _load_settings(self):
            s = self._settings()
            if s.value("database"):
                cfg = {}
                for k in self.FIELD_KEYS:
                    val = s.value(k)
                    if val is not None and val != "":
                        cfg[k] = val
                for b in ("create_missing_tiers", "match_famille_par_intitule",
                          "create_missing_familles", "calc_prix_achat_ttc",
                          "barcode_depuis_ref", "prix_vente_auto",
                          "match_par_designation", "ref_dans_designation",
                      "maj_code_barres"):
                    v = s.value(b)
                    if v is not None:
                        cfg[b] = str(v).lower() in ("true", "1")
                self._apply_config_dict(cfg)
                mv = s.value("marge_pct")
                if mv is not None:
                    try: self.f_marge.setValue(float(mv))
                    except (TypeError, ValueError): pass
                at = s.value("arrondi_paliers_text")
                if at:
                    self.f_arrondi.setText(str(at))
            last_excel = s.value("last_excel")
            if last_excel and os.path.isfile(last_excel):
                self.excel_edit.setText(last_excel)
                self.load_preview(last_excel)

        def _save_settings(self):
            s = self._settings(); cfg = self._build_config_dict()
            for k in self.FIELD_KEYS:
                s.setValue(k, cfg.get(k, ""))
            for b in ("create_missing_tiers", "match_famille_par_intitule",
                      "create_missing_familles", "calc_prix_achat_ttc",
                      "barcode_depuis_ref", "prix_vente_auto",
                      "match_par_designation"):
                s.setValue(b, bool(cfg.get(b)))
            s.setValue("marge_pct", float(cfg.get("marge_pct", 50)))
            s.setValue("arrondi_paliers_text", self.f_arrondi.text())
            if self.excel_edit.text().strip():
                s.setValue("last_excel", self.excel_edit.text().strip())

        def closeEvent(self, ev):
            try:
                self._save_settings()
            finally:
                super().closeEvent(ev)

        def _refresh_script_banner(self):
            if self.script_path:
                msg = "Outil detecte : <code>%s</code>" % self.script_path
                if self.tool_err:
                    msg += ("<br><span style='color:#a15c00;'>Apercu hors-ligne indisponible "
                            "(%s).</span>" % friendly_error(self.tool_err))
                self.script_banner.setStyleSheet(
                    "color:#235; background:#eef3fb; padding:6px; border-radius:4px;")
            else:
                msg = ("<b>Outil introuvable.</b> Placez <code>%s</code> dans le meme dossier, "
                       "ou <a href='#'>cliquez ici pour le localiser</a>." % SCRIPT_NAME)
                self.script_banner.setStyleSheet(
                    "color:#7a1f1f; background:#fbeeee; padding:6px; border-radius:4px;")
            self.script_banner.setText(msg)

        # ---- selecteurs ------------------------------------------------- #
        def locate_script(self, *_):
            path, _f = QFileDialog.getOpenFileName(self, "Localiser %s" % SCRIPT_NAME, "", "Python (*.py)")
            if path:
                self.script_path = path
                self.tool_mod, self.tool_err = load_tool_module(path)
                self._apply_defaults_from_tool(); self._refresh_script_banner()

        def pick_excel(self):
            path, _f = QFileDialog.getOpenFileName(
                self, "Choisir le fichier fournisseur (Excel ou PDF)", "",
                "Fournisseur (*.xlsx *.xlsm *.pdf);;Excel (*.xlsx *.xlsm);;PDF (*.pdf)")
            if path:
                self.excel_edit.setText(path); self.load_preview(path)

        def _read_source(self, path, cfg):
            """Lit le fichier fournisseur selon son extension (PDF ou Excel)."""
            if path.lower().endswith(".pdf"):
                return self.tool_mod.read_pdf(path, cfg)
            return self.tool_mod.read_excel(path, cfg)

        def pick_fdb(self):
            path, _f = QFileDialog.getOpenFileName(self, "Choisir la base", "", "Firebird (*.fdb *.FDB);;Tous (*)")
            if path:
                self.f_database.setText(path)

        def pick_fbclient(self):
            path, _f = QFileDialog.getOpenFileName(self, "Choisir fbclient", "", "Librairies (*.dll *.so *.dylib);;Tous (*)")
            if path:
                self.f_fbclient.setText(path)

        # ---- apercu hors-ligne ----------------------------------------- #
        def load_preview(self, path):
            self.preview_table.setRowCount(0)
            self._pv_manual.clear()
            self._match_rows = {}
            if not self.tool_mod:
                self.preview_count.setText(
                    "Apercu hors-ligne indisponible. Utilisez « Apercu (dry-run) ».")
                return
            is_pdf = path.lower().endswith(".pdf")
            if is_pdf:
                self.preview_count.setText("Lecture du PDF en cours…")
                QApplication.processEvents()
            try:
                base = json.loads(json.dumps(getattr(self.tool_mod, "DEFAULT_CONFIG", {})))
                base.update(self._build_config_dict())
                lines = self._read_source(path, base)
            except Exception as exc:  # noqa: BLE001
                self.preview_count.setText("Lecture impossible : %s" % friendly_error(str(exc)))
                return
            self._fill_table_from_lines(lines, base)
            bad = self._flag_recon(lines)
            msg = ("%d ligne(s)  ·  Total HT indicatif apres apercu dry-run)" % len(lines))
            if is_pdf:
                msg = ("PDF : %d ligne(s) lue(s)" % len(lines)
                       + (" · %d a verifier (montant)" % bad if bad else " · montants OK"))
            charset = str(base.get("charset", "")).upper()
            desigs = [ln.get("designation") or "" for ln in lines]

            def _nonlatin(s):
                try:
                    s.encode("latin-1"); return False
                except UnicodeEncodeError:
                    return True

            has_q = any("?" in d for d in desigs)
            has_arabic = any(_nonlatin(d) for d in desigs)
            warn = ""
            if has_q and charset not in ("WIN1256", "UTF8", "UNICODE_FSS"):
                warn = (" | Caracteres « ? » : charset « %s » ne gere pas l'arabe -> WIN1256" % charset)
            elif has_arabic and charset in ("UTF8", "UNICODE_FSS"):
                warn = (" | ARABE avec « %s » sera deforme dans le logiciel -> WIN1256" % charset)
            self.preview_count.setStyleSheet(
                "color:#7a1f1f; font-weight:bold;" if warn else "color:#555;")
            self.preview_count.setText(msg + warn)
            # Mettre a jour le N° du bon si non renseigne et refdoc par defaut vide
            if not self.f_refdoc.text().strip():
                fname = os.path.splitext(os.path.basename(path))[0]
                # Ne pre-remplir qu'a titre indicatif, l'utilisateur peut effacer
                pass

        def _fill_table_from_lines(self, lines, base):
            """Remplit la table depuis une liste de lignes (sans effacer les edits precedents)."""
            auto = bool(base.get("prix_vente_auto"))
            marge = float(base.get("marge_pct", 50) or 0)
            tiers = base.get("arrondi_paliers") or [[200, 5], [1000, 10], [None, 50]]
            round_fn = getattr(self.tool_mod, "round_price_up", None) if self.tool_mod else None

            with self._suspend_cellchanged():
                self.preview_table.setRowCount(len(lines))
                for r, ln in enumerate(lines):
                    pa = float(ln["prix"])
                    if auto and round_fn:
                        pv = round_fn(pa * (1 + marge / 100.0), tiers)
                    else:
                        pv = 0.0
                    ht = float(ln["qte"]) * pa

                    vals = [
                        ln["ref_art"],
                        ln["designation"],
                        self._fmt(ln["qte"]),
                        self._fmt(pa),
                        self._fmt(pv) if pv else "",
                        self._fmt(ln["tva"]),
                        ln.get("famille") or "",
                        ln.get("code_barres") or "",
                        "",           # MAJ PA? — vide au depart
                        "",           # Statut
                        "",           # Match
                        self._fmt(ht),
                    ]
                    for c, txt in enumerate(vals):
                        it = QTableWidgetItem(str(txt))
                        # Colonnes lecture-seule : Statut, Match, Total HT
                        if c in (self.COL_STATUT, self.COL_MATCH, self.COL_TOTAL):
                            it.setFlags(it.flags() & ~Qt.ItemFlag.ItemIsEditable)
                        elif c == self.COL_MAJ_PA:
                            it.setFlags(Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsEnabled)
                            it.setCheckState(Qt.CheckState.Unchecked)
                        else:
                            it.setFlags(it.flags() | Qt.ItemFlag.ItemIsEditable)
                        if c in (self.COL_QTE, self.COL_PA, self.COL_PV,
                                 self.COL_TVA, self.COL_TOTAL):
                            it.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
                        self.preview_table.setItem(r, c, it)
            self._flag_ocr_anomalies()

        @contextlib.contextmanager
        def _suspend_cellchanged(self):
            """Gestionnaire de contexte qui suspend le handler cellChanged."""
            old = self._cc_guard
            self._cc_guard = True
            try:
                yield
            finally:
                self._cc_guard = old

        def _on_cell_changed(self, row, col):
            """Recompute prix vente / total quand une cellule est editee."""
            if self._cc_guard:
                return
            with self._suspend_cellchanged():
                self._recompute_row(row, col)
            self._flag_ocr_anomalies()

        def _recompute_row(self, row, col):
            """Recalcule PV et/ou Total pour la ligne 'row' apres edition de 'col'."""
            def cell(c):
                it = self.preview_table.item(row, c)
                return it.text() if it else ""

            def set_cell(c, v):
                it = self.preview_table.item(row, c)
                if it:
                    it.setText(v)
                else:
                    nit = QTableWidgetItem(v)
                    nit.setFlags(nit.flags() & ~Qt.ItemFlag.ItemIsEditable)
                    self.preview_table.setItem(row, c, nit)

            if self.tool_mod:
                tof = getattr(self.tool_mod, "to_float", float)
            else:
                tof = lambda v, d=0.0: float(str(v).replace(",", ".") or d)

            pa  = tof(cell(self.COL_PA), 0.0)
            qte = tof(cell(self.COL_QTE), 0.0)
            tva = tof(cell(self.COL_TVA), 0.0)

            if col == self.COL_PV:
                # Utilisateur a edite PV manuellement -> marquer
                self._pv_manual.add(row)
            elif col == self.COL_PA and row not in self._pv_manual:
                # Recalculer PV depuis PA
                if self.f_prix_vente_auto.isChecked() and pa > 0:
                    marge = float(self.f_marge.value())
                    paliers = parse_arrondi(self.f_arrondi.text())
                    if self.tool_mod:
                        round_fn = getattr(self.tool_mod, "round_price_up", None)
                        if round_fn:
                            pv = round_fn(pa * (1 + marge / 100.0), paliers)
                            set_cell(self.COL_PV, self._fmt(pv))

            # Total HT = PA * Qte (sans TVA dans la colonne Total, coherent avec l'outil)
            total = pa * qte
            set_cell(self.COL_TOTAL, self._fmt(total))

        def _recompute_all_pv(self):
            """Recalcule les PV de toutes les lignes NON saisies manuellement."""
            if not self.tool_mod:
                return
            auto = self.f_prix_vente_auto.isChecked()
            marge = float(self.f_marge.value())
            paliers = parse_arrondi(self.f_arrondi.text())
            round_fn = getattr(self.tool_mod, "round_price_up", None)
            tof = getattr(self.tool_mod, "to_float", None)
            if not tof:
                return

            with self._suspend_cellchanged():
                for r in range(self.preview_table.rowCount()):
                    if r in self._pv_manual:
                        continue
                    pa_it = self.preview_table.item(r, self.COL_PA)
                    if not pa_it:
                        continue
                    pa = tof(pa_it.text(), 0.0)
                    qte_it = self.preview_table.item(r, self.COL_QTE)
                    qte = tof(qte_it.text() if qte_it else "0", 0.0)
                    if auto and round_fn and pa > 0:
                        pv = round_fn(pa * (1 + marge / 100.0), paliers)
                        pv_it = self.preview_table.item(r, self.COL_PV)
                        if pv_it:
                            pv_it.setText(self._fmt(pv))
                    # Recalcule aussi le total
                    tot_it = self.preview_table.item(r, self.COL_TOTAL)
                    if tot_it:
                        tot_it.setText(self._fmt(pa * qte))

        def _flag_ocr_anomalies(self):
            """Colore en orange les lignes avec qte<=0 ou prix<=0."""
            seuil_pct = float(getattr(self, "f_maj_seuil", None) and self.f_maj_seuil.value() or 40)
            warnings = 0
            tof = getattr(self.tool_mod, "to_float", None) if self.tool_mod else None
            if not tof:
                return
            nb = self.preview_table.rowCount()
            for r in range(nb):
                pa_it = self.preview_table.item(r, self.COL_PA)
                qte_it = self.preview_table.item(r, self.COL_QTE)
                stat_it = self.preview_table.item(r, self.COL_STATUT)
                match_it = self.preview_table.item(r, self.COL_MATCH)
                pa  = tof(pa_it.text() if pa_it else "0", 0.0)
                qte = tof(qte_it.text() if qte_it else "0", 0.0)
                # Lire match_prix_achat si stocke dans la cellule Match (format "REF | PA=XXX")
                match_pa = None
                if match_it and match_it.text():
                    m = re.search(r"PA=([\d.]+)", match_it.text())
                    if m:
                        try: match_pa = float(m.group(1))
                        except ValueError: pass
                bad_ocr = (qte <= 0 or pa <= 0)
                bad_prix = False
                if match_pa and pa > 0 and seuil_pct > 0:
                    diff_pct = abs(pa - match_pa) / match_pa * 100 if match_pa else 0
                    bad_prix = diff_pct > seuil_pct
                if bad_ocr or bad_prix:
                    warnings += 1
                    if stat_it:
                        stat_it.setText("ANOMALIE")
                        stat_it.setBackground(COLOR_WARN)
            # Afficher le compte en tete de table
            if warnings > 0:
                self.preview_count.setStyleSheet("color:#7a1f1f; font-weight:bold;")
                cur = self.preview_count.text().split(" | ")[0]
                self.preview_count.setText(cur + " | %d anomalie(s) OCR detectee(s)" % warnings)

        def _flag_recon(self, lines):
            """Colore (orange) les lignes d'un PDF dont Qte x Prix != Montant —
            signe d'une lecture douteuse a verifier. Retourne le nombre."""
            bad = 0
            with self._suspend_cellchanged():
                for r, ln in enumerate(lines):
                    if r >= self.preview_table.rowCount():
                        break
                    if ln.get("recon") is False:
                        bad += 1
                        for c in range(self.preview_table.columnCount()):
                            it = self.preview_table.item(r, c)
                            if it:
                                it.setBackground(COLOR_WARN)
                        st = self.preview_table.item(r, self.COL_STATUT)
                        if st:
                            st.setText("VERIF MONTANT")
            return bad

        # ---- collecte des lignes depuis la table ------------------------ #
        def _collect_lines_from_table(self):
            """Lit chaque ligne de la table et retourne la liste de dicts."""
            tof = getattr(self.tool_mod, "to_float", None) if self.tool_mod else None
            if not tof:
                def tof(v, d=0.0):
                    try: return float(str(v).replace(",", "."))
                    except (TypeError, ValueError): return d

            rows = []
            nb = self.preview_table.rowCount()
            for r in range(nb):
                def cell(c):
                    it = self.preview_table.item(r, c)
                    return (it.text() if it else "").strip()

                ref = cell(self.COL_REF)
                if not ref:
                    continue
                d = {
                    "ref_art":     ref,
                    "designation": cell(self.COL_DESIG) or ref,
                    "qte":         tof(cell(self.COL_QTE), 0.0),
                    "prix":        tof(cell(self.COL_PA), 0.0),
                    "tva":         tof(cell(self.COL_TVA), 0.0),
                    "famille":     cell(self.COL_FAM),
                    "code_barres": cell(self.COL_CB),
                }
                # PV saisi manuellement
                if r in self._pv_manual:
                    pv = tof(cell(self.COL_PV), None)
                    if pv and pv > 0:
                        d["prix_vente"] = pv
                # MAJ prix achat (colonne checkable)
                maj_it = self.preview_table.item(r, self.COL_MAJ_PA)
                if maj_it and maj_it.checkState() == Qt.CheckState.Checked:
                    d["maj_prix_achat"] = True
                rows.append(d)
            return rows

        @staticmethod
        def _fmt(v):
            try:
                f = float(v)
            except (TypeError, ValueError):
                return str(v)
            return str(int(f)) if f == int(f) else ("%.2f" % f)

        # ---- config <-> formulaire ------------------------------------- #
        def _code_tiers_text(self):
            data = self.f_code_tiers.currentData()
            if data:
                return str(data)
            t = self.f_code_tiers.currentText().strip()
            return t.split(" — ")[0].strip()

        def _code_depot_text(self):
            data = self.f_code_depot.currentData()
            if data:
                return str(data)
            t = self.f_code_depot.currentText().strip()
            return t.split(" — ")[0].strip()

        def _default_famille_text(self):
            data = self.f_default_famille.currentData()
            if data:
                return str(data)
            return self.f_default_famille.currentText().strip() or "TOUS"

        def _build_config_dict(self):
            return {
                "host": self.f_host.text().strip(),
                "port": int(self.f_port.value()),
                "database": self.f_database.text().strip(),
                "user": self.f_user.text().strip(),
                "password": self.f_password.text(),
                "charset": self.f_charset.currentText().strip(),
                "fb_client_library": self.f_fbclient.text().strip() or None,
                "code_type_piece": self.f_code_type_piece.text().strip() or "PC_AC_B",
                "etat": None,
                "code_tiers": self._code_tiers_text(),
                "raison_sociale": self.f_raison.text().strip(),
                "code_depot": self._code_depot_text(),
                "create_missing_tiers": self.f_create_tiers.isChecked(),
                "default_famille": self._default_famille_text(),
                "default_famille_intitule": self.f_default_famille_int.text().strip() or "Tous",
                "default_unite": self.f_default_unite.text().strip(),
                "default_unite_intitule": self.f_default_unite_int.text().strip() or "Unite",
                "default_tva": float(self.f_default_tva.value()),
                "match_famille_par_intitule": self.f_match_famille.isChecked(),
                "create_missing_familles": self.f_create_familles.isChecked(),
                "calc_prix_achat_ttc": self.f_calc_ttc.isChecked(),
                "reserved_id_threshold": int(self.f_reserved.value()),
                "barcode_depuis_ref": self.f_barcode_ref.isChecked(),
                "colonne_prix": self.f_colonne_prix.currentText().strip() or "prix",
                "prix_vente_auto": self.f_prix_vente_auto.isChecked(),
                "marge_pct": float(self.f_marge.value()),
                "arrondi_paliers": parse_arrondi(self.f_arrondi.text()),
                "refdoc": self.f_refdoc.text().strip(),
                "match_par_designation": self.f_match_par_desig.isChecked(),
                "ref_dans_designation": self.f_ref_designation.isChecked(),
                "maj_code_barres": self.f_maj_code_barres.isChecked(),
                "maj_prix_achat_seuil_pct": float(self.f_maj_seuil.value()),
            }

        def _apply_config_dict(self, cfg):
            def s(key, widget):
                if key in cfg and cfg[key] is not None:
                    widget.setText(str(cfg[key]))

            def sc(key, widget):
                """setText sur un QComboBox editable."""
                if key in cfg and cfg[key] is not None:
                    widget.setCurrentText(str(cfg[key]))

            s("database", self.f_database); s("host", self.f_host)
            if "port" in cfg:
                try: self.f_port.setValue(int(cfg["port"]))
                except (TypeError, ValueError): pass
            s("user", self.f_user); s("password", self.f_password)
            if "charset" in cfg: self.f_charset.setCurrentText(str(cfg["charset"]))
            if "fb_client_library" in cfg:
                self.f_fbclient.setText(str(cfg.get("fb_client_library") or ""))
            s("code_type_piece", self.f_code_type_piece)
            sc("code_tiers", self.f_code_tiers); s("raison_sociale", self.f_raison)
            sc("code_depot", self.f_code_depot)
            if "create_missing_tiers" in cfg: self.f_create_tiers.setChecked(bool(cfg["create_missing_tiers"]))
            sc("default_famille", self.f_default_famille)
            s("default_famille_intitule", self.f_default_famille_int)
            if "default_unite" in cfg:
                self.f_default_unite.setText(str(cfg.get("default_unite") or ""))
            s("default_unite_intitule", self.f_default_unite_int)
            if "default_tva" in cfg:
                try: self.f_default_tva.setValue(float(cfg["default_tva"]))
                except (TypeError, ValueError): pass
            if "match_famille_par_intitule" in cfg: self.f_match_famille.setChecked(bool(cfg["match_famille_par_intitule"]))
            if "create_missing_familles" in cfg: self.f_create_familles.setChecked(bool(cfg["create_missing_familles"]))
            if "calc_prix_achat_ttc" in cfg: self.f_calc_ttc.setChecked(bool(cfg["calc_prix_achat_ttc"]))
            if "reserved_id_threshold" in cfg:
                try: self.f_reserved.setValue(int(cfg["reserved_id_threshold"]))
                except (TypeError, ValueError): pass
            if "barcode_depuis_ref" in cfg: self.f_barcode_ref.setChecked(bool(cfg["barcode_depuis_ref"]))
            if "colonne_prix" in cfg: self.f_colonne_prix.setCurrentText(str(cfg["colonne_prix"]))
            if "prix_vente_auto" in cfg: self.f_prix_vente_auto.setChecked(bool(cfg["prix_vente_auto"]))
            if "marge_pct" in cfg:
                try: self.f_marge.setValue(float(cfg["marge_pct"]))
                except (TypeError, ValueError): pass
            if cfg.get("arrondi_paliers"):
                self.f_arrondi.setText(format_arrondi(cfg["arrondi_paliers"]))
            if "refdoc" in cfg:
                self.f_refdoc.setText(str(cfg.get("refdoc") or ""))
            if "match_par_designation" in cfg:
                self.f_match_par_desig.setChecked(bool(cfg["match_par_designation"]))
            if "ref_dans_designation" in cfg:
                self.f_ref_designation.setChecked(bool(cfg["ref_dans_designation"]))
            if "maj_code_barres" in cfg:
                self.f_maj_code_barres.setChecked(bool(cfg["maj_code_barres"]))
            if "maj_prix_achat_seuil_pct" in cfg:
                try: self.f_maj_seuil.setValue(float(cfg["maj_prix_achat_seuil_pct"]))
                except (TypeError, ValueError): pass

        # ---- test connexion / chargement des listes -------------------- #
        def test_connection(self):
            cfg = self._build_config_dict()
            if not cfg["database"]:
                self._set_conn(False, "Renseignez le chemin de la base."); return
            if not self.tool_mod:
                self._set_conn(False, friendly_error(self.tool_err)); return
            if cfg.get("host"):
                self._set_conn(None, "Connexion au serveur en cours (peut figer "
                                     "quelques secondes si l'hote ne repond pas)…")
            else:
                self._set_conn(None, "Connexion en cours…")
            QApplication.processEvents()
            try:
                con = self.tool_mod.connect(cfg)
                con.close()
                self._set_conn(True, "Connexion reussie.")
                self.conn_result.setToolTip("")
            except SystemExit as exc:
                self._report_conn_error(str(exc.code))
            except Exception as exc:  # noqa: BLE001
                self._report_conn_error(str(exc))

        def _report_conn_error(self, raw):
            """Affiche le message lisible ET conserve l'erreur brute Firebird
            (infobulle + Journal) pour pouvoir diagnostiquer la vraie cause."""
            self._set_conn(False, friendly_error(raw))
            self.conn_result.setToolTip(raw or "")
            self._append_log("\n[Test connexion] ECHEC — message brut Firebird :\n"
                             "%s\n" % (raw or "(vide)"))

        def load_lists(self):
            """Charge les listes de familles/fournisseurs/depots depuis la base."""
            cfg = self._build_config_dict()
            if not cfg["database"]:
                QMessageBox.warning(self, "A corriger", "Renseignez le chemin de la base."); return
            if not self.script_path:
                QMessageBox.warning(self, "A corriger", "Outil introuvable."); return

            self._set_conn(None, "Chargement en cours…")
            QApplication.processEvents()
            try:
                fd, tmp_cfg = tempfile.mkstemp(suffix=".json", prefix="primenf_cfg_")
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    json.dump(cfg, fh, indent=2, ensure_ascii=False)
                _, tmp_fam = tempfile.mkstemp(suffix=".json", prefix="primenf_fam_")
                _, tmp_tiers = tempfile.mkstemp(suffix=".json", prefix="primenf_tiers_")

                mod, err = load_tool_module(self.script_path)
                if not mod:
                    self._set_conn(False, friendly_error(err)); return

                import argparse
                # Charger familles
                _args_fam = argparse.Namespace(
                    config=tmp_cfg, db=None, code_tiers=None, refdoc=None,
                    cancel_piece=None, list_familles=True, list_tiers=False,
                    match=False, check_dup=False, make_template=None,
                    excel=None, lines=None, out=tmp_fam, dry_run=False, date=None)
                mod.mode_list_familles(cfg, tmp_fam)
                mod.mode_list_tiers(cfg, tmp_tiers)

                fam_data = json.load(open(tmp_fam, encoding="utf-8"))
                tiers_data = json.load(open(tmp_tiers, encoding="utf-8"))

                self._familles_list = fam_data
                # Mettre a jour le delegate Famille de la table
                self._famille_delegate.set_familles(fam_data)

                # Remplir la combo famille par defaut
                cur_fam = self._default_famille_text()
                self.f_default_famille.clear()
                for f in fam_data:
                    prefix = "  " * f.get("depth", 0)
                    self.f_default_famille.addItem(prefix + f["intitule"], f["code"])
                # Ré-selectionner la valeur precedente
                idx = self.f_default_famille.findText(cur_fam)
                if idx >= 0:
                    self.f_default_famille.setCurrentIndex(idx)
                elif cur_fam:
                    self.f_default_famille.setCurrentText(cur_fam)

                # Remplir fournisseurs
                cur_tiers = self._code_tiers_text()
                self.f_code_tiers.clear()
                self.f_code_tiers.addItem("")
                for t in tiers_data.get("fournisseurs", []):
                    self.f_code_tiers.addItem("%s — %s" % (t["code"], t["raison"][:40]), t["code"])
                if cur_tiers:
                    # Chercher par code
                    found = False
                    for i in range(self.f_code_tiers.count()):
                        if self.f_code_tiers.itemData(i) == cur_tiers:
                            self.f_code_tiers.setCurrentIndex(i); found = True; break
                    if not found:
                        self.f_code_tiers.setCurrentText(cur_tiers)

                # Remplir depots
                cur_depot = self._code_depot_text()
                self.f_code_depot.clear()
                self.f_code_depot.addItem("")
                for t in tiers_data.get("depots", []):
                    self.f_code_depot.addItem("%s — %s" % (t["code"], t["raison"][:40]), t["code"])
                if cur_depot:
                    found = False
                    for i in range(self.f_code_depot.count()):
                        if self.f_code_depot.itemData(i) == cur_depot:
                            self.f_code_depot.setCurrentIndex(i); found = True; break
                    if not found:
                        self.f_code_depot.setCurrentText(cur_depot)

                self._set_conn(True, "Listes chargees : %d familles, %d fournisseurs, %d depots" % (
                    len(fam_data), len(tiers_data.get("fournisseurs", [])),
                    len(tiers_data.get("depots", []))))
            except Exception as exc:  # noqa: BLE001
                self._set_conn(False, "Chargement echoue : %s" % friendly_error(str(exc)))
            finally:
                for p in (tmp_cfg, tmp_fam, tmp_tiers):
                    try:
                        if os.path.isfile(p): os.remove(p)
                    except OSError:
                        pass

        def _set_conn(self, ok, msg):
            color = {True: "#1f7a33", False: "#7a1f1f", None: "#555"}[ok]
            mark = {True: "✓ ", False: "✗ ", None: ""}[ok]
            self.conn_result.setStyleSheet("color:%s;" % color)
            self.conn_result.setText(mark + msg)

        # ---- execution -------------------------------------------------- #
        def _validate(self):
            p = []
            if not self.script_path:
                p.append("L'outil import_bon_reception.py est introuvable.")
            if self.preview_table.rowCount() == 0:
                p.append("La table est vide. Chargez un fichier Excel ou saisissez des lignes.")
            if not self.f_database.text().strip():
                p.append("Renseignez le chemin de la base Firebird (.FDB).")
            d = self.f_date.text().strip()
            if d and not re.match(r"^\d{4}-\d{2}-\d{2}$", d):
                p.append("La date doit etre au format AAAA-MM-JJ.")
            return p

        def _write_tmp_config_and_lines(self):
            """Ecrit les fichiers temporaires config.json et lines.json.
            Retourne (cfg_path, lines_path) ou leve une exception."""
            lines = self._collect_lines_from_table()
            if not lines:
                raise ValueError("Aucune ligne dans la table.")
            cfg = self._build_config_dict()
            # Integrer le nom du fichier Excel dans refdoc si disponible
            excel = self.excel_edit.text().strip()
            if excel:
                base = os.path.basename(excel)
                refdoc = cfg.get("refdoc") or ""
                if refdoc:
                    cfg["refdoc"] = "%s (%s)" % (refdoc, base)
                # sinon on laisse vide ou on utilise juste le nom de fichier
                # selon preference (ne pas forcer le nom seul)
            fd, tmp_cfg = tempfile.mkstemp(suffix=".json", prefix="primenf_cfg_")
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(cfg, fh, indent=2, ensure_ascii=False)
            fd2, tmp_lines = tempfile.mkstemp(suffix=".json", prefix="primenf_lines_")
            with os.fdopen(fd2, "w", encoding="utf-8") as fh:
                json.dump(lines, fh, indent=2, ensure_ascii=False)
            return tmp_cfg, tmp_lines

        def run_import(self, dry_run):
            if self.proc is not None:
                return
            probs = self._validate()
            if probs:
                QMessageBox.warning(self, "A corriger", "\n".join("• " + x for x in probs)); return
            if not dry_run:
                bad = self._charset_arabic_problem()
                if bad:
                    r = QMessageBox.warning(
                        self, "Charset incorrect pour l'arabe",
                        bad + "\n\nImporter quand meme (deconseille) ?",
                        QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                        QMessageBox.StandardButton.No)
                    if r != QMessageBox.StandardButton.Yes:
                        return
                # Verification doublons avant import reel
                if not self._confirm_no_dup():
                    return
                ok = QMessageBox.question(
                    self, "Confirmer l'import reel",
                    "Cet import ECRIT en base (commit) et alimente le stock.\n\n"
                    "• Avez-vous fait un APERCU au prealable ?\n"
                    "• Avez-vous une SAUVEGARDE recente de la base ?\n\n"
                    "Relancer le meme fichier creerait un SECOND bon (stock double).\n\n"
                    "Lancer l'import reel ?",
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                    QMessageBox.StandardButton.No)
                if ok != QMessageBox.StandardButton.Yes:
                    return
            # Verification des anomalies OCR
            if not dry_run:
                n_warn = self._count_ocr_warnings()
                if n_warn > 0:
                    r = QMessageBox.warning(
                        self, "Anomalies detectees",
                        "%d ligne(s) avec des valeurs suspectes (Qte/Prix nul ou "
                        "ecart de prix excessif).\n\nImporter quand meme ?" % n_warn,
                        QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                        QMessageBox.StandardButton.No)
                    if r != QMessageBox.StandardButton.Yes:
                        return
            try:
                self.tmp_config_path, self.tmp_lines_path = self._write_tmp_config_and_lines()
            except Exception as exc:  # noqa: BLE001
                QMessageBox.critical(self, "Erreur", "Preparation : %s" % exc); return

            cli = ["--run-cli", "--config", self.tmp_config_path,
                   "--lines", self.tmp_lines_path]
            d = self.f_date.text().strip()
            if d:
                cli += ["--date", d]
            if dry_run:
                cli.append("--dry-run")
            if getattr(sys, "frozen", False):
                program, args = sys.executable, cli
            else:
                program, args = sys.executable, [GUI_SCRIPT] + cli

            self.current_mode = "dry" if dry_run else "real"
            self.log.clear()
            self._append_log("$ %s\n" % " ".join(self._q(a) for a in [program] + args))
            self._reset_summary(dry_run)

            self.proc = QProcess(self)
            self.proc.setProcessChannelMode(QProcess.ProcessChannelMode.MergedChannels)
            self.proc.readyReadStandardOutput.connect(self._on_output)
            self.proc.finished.connect(self._on_finished)
            self.proc.errorOccurred.connect(self._on_proc_error)
            self._set_running(True)
            self.status.setText("Apercu en cours…" if dry_run else "Import reel en cours…")
            self.proc.setProgram(program); self.proc.setArguments(args); self.proc.start()

        def _count_ocr_warnings(self):
            """Compte les lignes avec statut ANOMALIE."""
            count = 0
            for r in range(self.preview_table.rowCount()):
                it = self.preview_table.item(r, self.COL_STATUT)
                if it and it.text() == "ANOMALIE":
                    count += 1
            return count

        def _confirm_no_dup(self):
            """Lance --check-dup. Si doublons trouves, demande confirmation.
            Retourne True si on peut continuer, False si annule."""
            if not self.script_path:
                return True
            try:
                tmp_cfg, tmp_lines = self._write_tmp_config_and_lines()
                _, tmp_out = tempfile.mkstemp(suffix=".json", prefix="primenf_dup_")
                mod, err = load_tool_module(self.script_path)
                if not mod:
                    return True
                cfg = json.load(open(tmp_cfg, encoding="utf-8"))
                lines = json.load(open(tmp_lines, encoding="utf-8"))
                mod.mode_check_dup(cfg, lines, tmp_out)
                result = json.load(open(tmp_out, encoding="utf-8"))
                dups = result.get("duplicates", [])
                if not dups:
                    return True
                # Afficher les doublons
                detail = "\n".join(
                    "  • %s (%s, HT=%.2f, N°=%s)" % (
                        d.get("ref_piece") or d.get("nopiece"),
                        str(d.get("date") or "")[:10],
                        d.get("montant_ht") or 0,
                        d.get("refdoc") or "")
                    for d in dups[:5])
                r = QMessageBox.warning(
                    self, "Doublons potentiels",
                    "%d bon(s) similaire(s) deja present(s) :\n%s\n\n"
                    "Continuer quand meme ?" % (len(dups), detail),
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                    QMessageBox.StandardButton.No)
                return r == QMessageBox.StandardButton.Yes
            except Exception:  # noqa: BLE001
                return True  # en cas d'erreur, ne pas bloquer
            finally:
                for p in [v for v in (tmp_cfg, tmp_lines, tmp_out) if v]:
                    try:
                        if os.path.isfile(p): os.remove(p)
                    except OSError:
                        pass

        def _charset_arabic_problem(self):
            """Message si le fichier contient de l'arabe mais que le charset
            choisi va le deformer ; '' sinon."""
            path = self.excel_edit.text().strip()
            if not path or not os.path.isfile(path) or not self.tool_mod:
                return ""
            try:
                cfg = json.loads(json.dumps(getattr(self.tool_mod, "DEFAULT_CONFIG", {})))
                cfg.update(self._build_config_dict()); cfg["charset"] = "UTF8"
                lines = self._read_source(path, cfg)
            except Exception:  # noqa: BLE001
                return ""
            has_arabic = any(any(ord(c) >= 0x100 for c in (ln.get("designation") or ""))
                             for ln in lines)
            cs = self.f_charset.currentText().strip().upper()
            if not has_arabic or cs == "WIN1256":
                return ""
            if cs in ("UTF8", "UNICODE_FSS"):
                return ("Ce fichier contient de l'ARABE et le charset est « %s » : l'apercu "
                        "affiche l'arabe, mais votre logiciel l'affichera DEFORME (lettres "
                        "accentuees). Choisissez WIN1256." % cs)
            return ("Ce fichier contient de l'ARABE mais le charset « %s » ne le gere pas "
                    "(il deviendra « ? »). Choisissez WIN1256." % cs)

        @staticmethod
        def _q(a):
            return '"%s"' % a if " " in a else a

        def cancel_run(self):
            if self.proc is not None:
                self.proc.kill(); self.status.setText("Arret demande…")

        def _on_proc_error(self, _e):
            self._append_log("\n[ERREUR] Lancement du processus impossible.\n")

        def _on_output(self):
            if self.proc is None:
                return
            self._append_log(bytes(self.proc.readAllStandardOutput()).decode("utf-8", "replace"))

        def _append_log(self, text):
            self.log.moveCursor(QTextCursor.MoveOperation.End)
            self.log.insertPlainText(text)
            self.log.moveCursor(QTextCursor.MoveOperation.End)

        def _on_finished(self, code, _st):
            text = self.log.toPlainText()
            self._parse_summary(text, code)
            # Nettoyer les fichiers temporaires
            for p in (self.tmp_config_path, self.tmp_lines_path):
                if p and os.path.isfile(p):
                    try: os.remove(p)
                    except OSError: pass
            self.tmp_config_path = None
            self.tmp_lines_path = None
            self.proc = None
            self._set_running(False)
            self.status.setText("Termine (code 0)." if code == 0
                                else "Termine avec erreurs (code %s) — voir Journal." % code)
            # Activer le bouton "Annuler" si import reel reussi
            if code == 0 and self.current_mode == "real" and self._last_nopiece:
                self.btn_undo.setEnabled(True)
                self.btn_undo.setToolTip("Annuler NOPIECE=%s" % self._last_nopiece)

        # ----- Rapprochement (--match) -----
        def run_match_action(self):
            """Lance le rapprochement des lignes de la table contre la base."""
            if self.proc is not None:
                return
            if self.preview_table.rowCount() == 0:
                QMessageBox.warning(self, "Table vide", "Chargez un fichier Excel d'abord."); return
            if not self.f_database.text().strip():
                QMessageBox.warning(self, "A corriger", "Renseignez la base Firebird (.FDB)."); return
            if not self.script_path:
                QMessageBox.warning(self, "A corriger", "Outil introuvable."); return
            try:
                self.tmp_config_path, self.tmp_lines_path = self._write_tmp_config_and_lines()
            except Exception as exc:  # noqa: BLE001
                QMessageBox.critical(self, "Erreur", "Preparation : %s" % exc); return

            _, self._match_out_path = tempfile.mkstemp(suffix=".json", prefix="primenf_match_")
            cli = ["--run-cli", "--config", self.tmp_config_path,
                   "--lines", self.tmp_lines_path, "--match", "--out", self._match_out_path]
            if getattr(sys, "frozen", False):
                program, args = sys.executable, cli
            else:
                program, args = sys.executable, [GUI_SCRIPT] + cli

            self.log.clear()
            self._append_log("$ %s\n" % " ".join(self._q(a) for a in [program] + args))
            self.proc = QProcess(self)
            self.proc.setProcessChannelMode(QProcess.ProcessChannelMode.MergedChannels)
            self.proc.readyReadStandardOutput.connect(self._on_output)
            self.proc.finished.connect(self._on_match_finished)
            self.proc.errorOccurred.connect(self._on_proc_error)
            self._set_running(True)
            self.status.setText("Rapprochement en cours…")
            self.proc.setProgram(program); self.proc.setArguments(args); self.proc.start()

        def _on_match_finished(self, code, _st):
            self.proc = None
            self._set_running(False)
            for p in (self.tmp_config_path, self.tmp_lines_path):
                if p and os.path.isfile(p):
                    try: os.remove(p)
                    except OSError: pass
            self.tmp_config_path = None; self.tmp_lines_path = None

            if code != 0:
                self.status.setText("Rapprochement echoue — voir Journal."); return

            try:
                result = json.load(open(self._match_out_path, encoding="utf-8"))
                self._apply_match_results(result)
                self.status.setText("Rapprochement termine.")
            except Exception as exc:  # noqa: BLE001
                self.status.setText("Erreur lecture resultats : %s" % exc)
            finally:
                if os.path.isfile(self._match_out_path):
                    try: os.remove(self._match_out_path)
                    except OSError: pass

        def _apply_match_results(self, result):
            """Colore la table et remplit la colonne Statut/Match."""
            tof = getattr(self.tool_mod, "to_float", float) if self.tool_mod else float
            self._match_rows = {}

            with self._suspend_cellchanged():
                nb = self.preview_table.rowCount()
                for r in range(nb):
                    if r >= len(result):
                        break
                    row = result[r]
                    status = row.get("status", "new")
                    match_ref = row.get("match_ref") or ""
                    match_desig = row.get("match_designation") or ""
                    match_pa = row.get("match_prix_achat")
                    score = row.get("match_score", 0)

                    # Colorer la ligne
                    if status == "exact":
                        bg = COLOR_EXACT
                    elif status == "matched":
                        bg = COLOR_MATCHED
                        # Auto-remplir la ref avec la ref trouvee, en MEMORISANT la
                        # ref fournisseur d'origine pour pouvoir refuser le match.
                        ref_it = self.preview_table.item(r, self.COL_REF)
                        if ref_it and match_ref:
                            orig_ref = ref_it.text()
                            self._match_rows[r] = {"orig": orig_ref, "match": match_ref,
                                                   "state": "accepted"}
                            ref_it.setText(match_ref)
                    else:
                        bg = COLOR_NEW

                    for c in range(self.preview_table.columnCount()):
                        it = self.preview_table.item(r, c)
                        if it:
                            it.setBackground(bg)

                    # Colonne Statut
                    stat_it = self.preview_table.item(r, self.COL_STATUT)
                    if stat_it:
                        stat_it.setText(status)

                    # Colonne Match (ref + designation abrege + prix actuel)
                    match_it = self.preview_table.item(r, self.COL_MATCH)
                    if match_it:
                        if match_ref:
                            pa_str = ("PA=%.2f" % match_pa) if match_pa else ""
                            match_it.setText("%s %s %s" % (match_ref, match_desig[:30], pa_str))
                        else:
                            match_it.setText("")

                    # Alerte prix : si matched/exact et ecart > seuil
                    if status in ("exact", "matched") and match_pa:
                        pa_it = self.preview_table.item(r, self.COL_PA)
                        pa = tof(pa_it.text() if pa_it else "0", 0.0)
                        seuil = float(self.f_maj_seuil.value())
                        if pa > 0 and seuil > 0:
                            diff_pct = abs(pa - match_pa) / match_pa * 100 if match_pa else 0
                            if diff_pct > seuil:
                                if stat_it:
                                    stat_it.setText("ALERTE PRIX +%.0f%%" % diff_pct)
                                    stat_it.setBackground(COLOR_WARN)

            self._flag_ocr_anomalies()
            if self._match_rows:
                self.status.setText(
                    "Rapprochement termine. Clic droit sur une ligne jaune pour "
                    "refuser un mauvais rapprochement (creer un nouvel article).")

        # ----- Refuser / accepter un rapprochement (clic droit) -----
        def _show_table_menu(self, pos):
            idx = self.preview_table.indexAt(pos)
            if not idx.isValid():
                return
            r = idx.row()
            info = self._match_rows.get(r)
            menu = QMenu(self)
            if info and info.get("state") == "accepted":
                a = menu.addAction("Ce n'est pas le meme article — creer un nouvel article")
                a.triggered.connect(lambda _=False, row=r: self._reject_match(row))
            elif info and info.get("state") == "rejected":
                a = menu.addAction("Finalement, utiliser l'article existant trouve (%s)"
                                   % info["match"])
                a.triggered.connect(lambda _=False, row=r: self._accept_match(row))
            else:
                a = menu.addAction("Aucun rapprochement a modifier sur cette ligne")
                a.setEnabled(False)
            menu.exec(self.preview_table.viewport().mapToGlobal(pos))

        def _set_row_color(self, r, color):
            with self._suspend_cellchanged():
                for c in range(self.preview_table.columnCount()):
                    it = self.preview_table.item(r, c)
                    if it:
                        it.setBackground(color)

        def _reject_match(self, row):
            """Refuse le rapprochement : on remet la REFERENCE FOURNISSEUR d'origine
            (un nouvel article sera cree) sans avoir a la ressaisir."""
            info = self._match_rows.get(row)
            if not info:
                return
            with self._suspend_cellchanged():
                ref_it = self.preview_table.item(row, self.COL_REF)
                if ref_it:
                    ref_it.setText(info["orig"])
                stat_it = self.preview_table.item(row, self.COL_STATUT)
                if stat_it:
                    stat_it.setText("nouveau (refuse)")
            info["state"] = "rejected"
            self._set_row_color(row, COLOR_NEW)
            self._flag_ocr_anomalies()
            self.status.setText("Ligne %d : rapprochement refuse — un nouvel article "
                                "sera cree (ref. %s)." % (row + 1, info["orig"]))

        def _accept_match(self, row):
            """Re-applique le rapprochement (revient sur un refus)."""
            info = self._match_rows.get(row)
            if not info:
                return
            with self._suspend_cellchanged():
                ref_it = self.preview_table.item(row, self.COL_REF)
                if ref_it:
                    ref_it.setText(info["match"])
                stat_it = self.preview_table.item(row, self.COL_STATUT)
                if stat_it:
                    stat_it.setText("matched")
            info["state"] = "accepted"
            self._set_row_color(row, COLOR_MATCHED)
            self._flag_ocr_anomalies()
            self.status.setText("Ligne %d : article existant utilise (ref. %s)."
                                % (row + 1, info["match"]))

        # ----- Annuler le dernier import -----
        def cancel_last_import(self):
            if not self._last_nopiece:
                QMessageBox.warning(self, "Aucun import", "Aucun import enregistre dans cette session.")
                return
            ok = QMessageBox.question(
                self, "Confirmer l'annulation",
                "Annuler le bon NOPIECE=%s ?\n\nCela marque le bon comme ANNULE "
                "et le stock sera repris." % self._last_nopiece,
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No)
            if ok != QMessageBox.StandardButton.Yes:
                return
            if not self.script_path:
                QMessageBox.warning(self, "Erreur", "Outil introuvable."); return
            try:
                fd, tmp_cfg = tempfile.mkstemp(suffix=".json", prefix="primenf_cfg_")
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    json.dump(self._build_config_dict(), fh, indent=2, ensure_ascii=False)
            except Exception as exc:  # noqa: BLE001
                QMessageBox.critical(self, "Erreur", "Config temporaire : %s" % exc); return

            cli = ["--run-cli", "--config", tmp_cfg, "--cancel-piece", self._last_nopiece]
            if getattr(sys, "frozen", False):
                program, args = sys.executable, cli
            else:
                program, args = sys.executable, [GUI_SCRIPT] + cli

            self.log.clear()
            self._append_log("$ %s\n" % " ".join(self._q(a) for a in [program] + args))
            self._undo_tmp_cfg = tmp_cfg

            self.proc = QProcess(self)
            self.proc.setProcessChannelMode(QProcess.ProcessChannelMode.MergedChannels)
            self.proc.readyReadStandardOutput.connect(self._on_output)
            self.proc.finished.connect(self._on_undo_finished)
            self.proc.errorOccurred.connect(self._on_proc_error)
            self._set_running(True)
            self.status.setText("Annulation en cours…")
            self.proc.setProgram(program); self.proc.setArguments(args); self.proc.start()

        def _on_undo_finished(self, code, _st):
            self.proc = None
            self._set_running(False)
            if hasattr(self, "_undo_tmp_cfg") and self._undo_tmp_cfg:
                try:
                    if os.path.isfile(self._undo_tmp_cfg): os.remove(self._undo_tmp_cfg)
                except OSError: pass
                self._undo_tmp_cfg = None
            if code == 0:
                self.status.setText("Annulation effectuee (NOPIECE=%s)." % self._last_nopiece)
                self._last_nopiece = None
                self.btn_undo.setEnabled(False)
            else:
                self.status.setText("Annulation echouee (code %s) — voir Journal." % code)

        # ----- Creer un modele Excel -----
        def make_template_action(self):
            path, _ = QFileDialog.getSaveFileName(
                self, "Creer un modele Excel", "MODELE_IMPORT.xlsx", "Excel (*.xlsx)")
            if not path:
                return
            if not self.script_path:
                QMessageBox.warning(self, "Erreur", "Outil introuvable."); return
            try:
                fd, tmp_cfg = tempfile.mkstemp(suffix=".json", prefix="primenf_cfg_")
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    json.dump(self._build_config_dict(), fh, indent=2, ensure_ascii=False)
            except Exception as exc:  # noqa: BLE001
                QMessageBox.critical(self, "Erreur", "Config temporaire : %s" % exc); return

            cli = ["--run-cli", "--make-template", path]
            if getattr(sys, "frozen", False):
                program, args = sys.executable, cli
            else:
                program, args = sys.executable, [GUI_SCRIPT] + cli

            self.log.clear()
            self._append_log("$ %s\n" % " ".join(self._q(a) for a in [program] + args))
            self._template_tmp_cfg = tmp_cfg

            self.proc = QProcess(self)
            self.proc.setProcessChannelMode(QProcess.ProcessChannelMode.MergedChannels)
            self.proc.readyReadStandardOutput.connect(self._on_output)
            self.proc.finished.connect(self._on_template_finished)
            self.proc.errorOccurred.connect(self._on_proc_error)
            self._set_running(True)
            self.proc.setProgram(program); self.proc.setArguments(args); self.proc.start()

        def _on_template_finished(self, code, _st):
            self.proc = None
            self._set_running(False)
            if hasattr(self, "_template_tmp_cfg") and self._template_tmp_cfg:
                try:
                    if os.path.isfile(self._template_tmp_cfg): os.remove(self._template_tmp_cfg)
                except OSError: pass
                self._template_tmp_cfg = None
            if code == 0:
                self.status.setText("Modele Excel cree.")
                QMessageBox.information(self, "Modele cree", "Le modele a ete cree avec succes.")
            else:
                self.status.setText("Erreur creation modele — voir Journal.")

        # ----- Synchroniser les codes-barres (articles deja importes) -----
        def run_sync_barcodes_action(self):
            if self.proc is not None:
                return
            if not self.f_database.text().strip():
                QMessageBox.warning(self, "A corriger", "Renseignez la base Firebird (.FDB)."); return
            if not self.script_path:
                QMessageBox.warning(self, "A corriger", "Outil introuvable."); return
            ok = QMessageBox.question(
                self, "Synchroniser les codes-barres",
                "Rendre visibles dans la fiche article les codes-barres des "
                "articles deja importes : lit les DEUX champs (CODE_BARRES et "
                "CODE_BARRE) et les recopie vers EQUIV_CBARRES.\n\n"
                "Operation sans risque (aucune suppression, rien sur le stock) et "
                "repetable. Sauvegardez tout de meme la base au prealable.\n\nLancer ?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No)
            if ok != QMessageBox.StandardButton.Yes:
                return
            try:
                fd, tmp_cfg = tempfile.mkstemp(suffix=".json", prefix="primenf_cfg_")
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    json.dump(self._build_config_dict(), fh, indent=2, ensure_ascii=False)
            except Exception as exc:  # noqa: BLE001
                QMessageBox.critical(self, "Erreur", "Config temporaire : %s" % exc); return

            cli = ["--run-cli", "--config", tmp_cfg, "--sync-barcodes"]
            if getattr(sys, "frozen", False):
                program, args = sys.executable, cli
            else:
                program, args = sys.executable, [GUI_SCRIPT] + cli

            self.log.clear()
            self._append_log("$ %s\n" % " ".join(self._q(a) for a in [program] + args))
            self._sync_tmp_cfg = tmp_cfg

            self.proc = QProcess(self)
            self.proc.setProcessChannelMode(QProcess.ProcessChannelMode.MergedChannels)
            self.proc.readyReadStandardOutput.connect(self._on_output)
            self.proc.finished.connect(self._on_sync_barcodes_finished)
            self.proc.errorOccurred.connect(self._on_proc_error)
            self._set_running(True)
            self.status.setText("Synchronisation des codes-barres…")
            self.proc.setProgram(program); self.proc.setArguments(args); self.proc.start()

        def _on_sync_barcodes_finished(self, code, _st):
            text = self.log.toPlainText()
            self.proc = None
            self._set_running(False)
            if hasattr(self, "_sync_tmp_cfg") and self._sync_tmp_cfg:
                try:
                    if os.path.isfile(self._sync_tmp_cfg): os.remove(self._sync_tmp_cfg)
                except OSError: pass
                self._sync_tmp_cfg = None
            if code == 0:
                m = re.search(r"(\d+)\s+ajoute", text)
                n = m.group(1) if m else "?"
                self.status.setText("Codes-barres synchronises (%s ajoute(s))." % n)
                QMessageBox.information(
                    self, "Codes-barres synchronises",
                    "%s code(s)-barres rendu(s) visible(s) dans la fiche article." % n)
            else:
                self.status.setText("Synchronisation echouee (code %s) — voir Journal." % code)

        # ----- Appliquer les codes-barres du fichier/table (sans bon, sans stock) -----
        def run_apply_barcodes_action(self):
            if self.proc is not None:
                return
            if not self.f_database.text().strip():
                QMessageBox.warning(self, "A corriger", "Renseignez la base Firebird (.FDB)."); return
            if self.preview_table.rowCount() == 0:
                QMessageBox.warning(self, "Table vide",
                                    "Chargez d'abord le fichier Excel fournisseur (avec ses codes-barres)."); return
            if not self.script_path:
                QMessageBox.warning(self, "A corriger", "Outil introuvable."); return
            ok = QMessageBox.question(
                self, "Appliquer les codes-barres",
                "Appliquer les codes-barres de la table aux articles EXISTANTS "
                "(EQUIV_CBARRES + CODE_BARRES/CODE_BARRE).\n\n"
                "AUCUN bon de reception n'est cree et le STOCK n'est PAS touche : "
                "c'est l'outil pour corriger d'anciens imports sans rien dupliquer.\n\n"
                "Sauvegardez la base au prealable. Lancer ?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No)
            if ok != QMessageBox.StandardButton.Yes:
                return
            try:
                self.tmp_config_path, self.tmp_lines_path = self._write_tmp_config_and_lines()
            except Exception as exc:  # noqa: BLE001
                QMessageBox.critical(self, "Erreur", "Preparation : %s" % exc); return

            cli = ["--run-cli", "--config", self.tmp_config_path,
                   "--lines", self.tmp_lines_path, "--apply-barcodes"]
            if getattr(sys, "frozen", False):
                program, args = sys.executable, cli
            else:
                program, args = sys.executable, [GUI_SCRIPT] + cli

            self.log.clear()
            self._append_log("$ %s\n" % " ".join(self._q(a) for a in [program] + args))

            self.proc = QProcess(self)
            self.proc.setProcessChannelMode(QProcess.ProcessChannelMode.MergedChannels)
            self.proc.readyReadStandardOutput.connect(self._on_output)
            self.proc.finished.connect(self._on_apply_barcodes_finished)
            self.proc.errorOccurred.connect(self._on_proc_error)
            self._set_running(True)
            self.status.setText("Application des codes-barres (sans stock)…")
            self.proc.setProgram(program); self.proc.setArguments(args); self.proc.start()

        def _on_apply_barcodes_finished(self, code, _st):
            text = self.log.toPlainText()
            self.proc = None
            self._set_running(False)
            for p in (self.tmp_config_path, self.tmp_lines_path):
                if p and os.path.isfile(p):
                    try: os.remove(p)
                    except OSError: pass
            self.tmp_config_path = None; self.tmp_lines_path = None
            if code == 0:
                m = re.search(r"appliques\s*:\s*(\d+)", text)
                nf = re.search(r"introuvables\s*:\s*(\d+)", text)
                n = m.group(1) if m else "?"
                extra = (" — %s article(s) introuvable(s)" % nf.group(1)) if (nf and nf.group(1) != "0") else ""
                self.status.setText("Codes-barres appliques : %s%s." % (n, extra))
                QMessageBox.information(
                    self, "Codes-barres appliques",
                    "%s code(s)-barres appliques aux articles existants%s.\n\n"
                    "Aucun bon cree, stock inchange." % (n, extra))
            else:
                self.status.setText("Application echouee (code %s) — voir Journal." % code)

        # ----- Nettoyage des articles corrompus « ? » -----
        def run_clean_action(self):
            if self.proc is not None:
                return
            if not self.f_database.text().strip():
                QMessageBox.warning(self, "A corriger", "Renseignez la base Firebird (.FDB)."); return
            if not self.script_path:
                QMessageBox.warning(self, "A corriger", "Outil import_bon_reception.py introuvable."); return
            try:
                fd, self.tmp_config_path = tempfile.mkstemp(suffix=".json", prefix="primenf_cfg_")
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    json.dump(self._build_config_dict(), fh, indent=2, ensure_ascii=False)
            except Exception as exc:  # noqa: BLE001
                QMessageBox.critical(self, "Erreur", "Config temporaire : %s" % exc); return
            self.log.clear()
            self._start_clean(apply=False)

        def _start_clean(self, apply):
            cli = ["--run-clean", "--config", self.tmp_config_path]
            if apply:
                cli += ["--apply", "--purge-pieces", "--purge-familles"]
            if getattr(sys, "frozen", False):
                program, args = sys.executable, cli
            else:
                program, args = sys.executable, [GUI_SCRIPT] + cli
            self.clean_apply = apply
            self._append_log("$ %s\n" % " ".join(self._q(a) for a in [program] + args))
            self.proc = QProcess(self)
            self.proc.setProcessChannelMode(QProcess.ProcessChannelMode.MergedChannels)
            self.proc.readyReadStandardOutput.connect(self._on_output)
            self.proc.finished.connect(self._on_clean_finished)
            self.proc.errorOccurred.connect(self._on_proc_error)
            self._set_running(True)
            self.status.setText("Nettoyage : suppression…" if apply else "Nettoyage : simulation…")
            self.proc.setProgram(program); self.proc.setArguments(args); self.proc.start()

        def _on_clean_finished(self, code, _st):
            text = self.log.toPlainText()
            self.proc = None
            self._set_running(False)
            if self.clean_apply:
                self.status.setText("Nettoyage termine (code 0)." if code == 0
                                    else "Nettoyage : erreur (code %s) — voir Journal." % code)
                self._cleanup_tmp()
                return
            # fin de la simulation
            if code != 0:
                self.status.setText("Nettoyage : erreur (voir Journal).")
                self._cleanup_tmp(); return
            m = re.search(r"corrompus[^:]*:\s*(\d+)", text)
            n = int(m.group(1)) if m else 0
            if n == 0:
                QMessageBox.information(self, "Nettoyage", "Aucun article corrompu (« ? ») trouve.")
                self.status.setText("Nettoyage : rien a supprimer."); self._cleanup_tmp(); return
            ok = QMessageBox.question(
                self, "Confirmer la suppression",
                "%d article(s) corrompu(s) trouve(s).\n\n"
                "Les supprimer DEFINITIVEMENT, avec leurs lignes liees (ITEM, TARIF…), "
                "les bons devenus vides et les familles « ? » sans article ?\n\n"
                "Sauvegardez la base au prealable." % n,
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No)
            if ok == QMessageBox.StandardButton.Yes:
                self._start_clean(apply=True)
            else:
                self.status.setText("Nettoyage annule."); self._cleanup_tmp()

        # ----- Reparation de l'encodage (import UTF-8 -> WIN1256) -----
        def run_repair_action(self):
            if self.proc is not None:
                return
            if not self.f_database.text().strip():
                QMessageBox.warning(self, "A corriger", "Renseignez la base Firebird (.FDB)."); return
            if not self.script_path:
                QMessageBox.warning(self, "A corriger", "Outil import_bon_reception.py introuvable."); return
            try:
                fd, self.tmp_config_path = tempfile.mkstemp(suffix=".json", prefix="primenf_cfg_")
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    json.dump(self._build_config_dict(), fh, indent=2, ensure_ascii=False)
            except Exception as exc:  # noqa: BLE001
                QMessageBox.critical(self, "Erreur", "Config temporaire : %s" % exc); return
            self.log.clear()
            self._start_repair(apply=False)

        def _start_repair(self, apply):
            cli = ["--run-repair", "--config", self.tmp_config_path]
            if apply:
                cli += ["--apply"]
            if getattr(sys, "frozen", False):
                program, args = sys.executable, cli
            else:
                program, args = sys.executable, [GUI_SCRIPT] + cli
            self.repair_apply = apply
            self._append_log("$ %s\n" % " ".join(self._q(a) for a in [program] + args))
            self.proc = QProcess(self)
            self.proc.setProcessChannelMode(QProcess.ProcessChannelMode.MergedChannels)
            self.proc.readyReadStandardOutput.connect(self._on_output)
            self.proc.finished.connect(self._on_repair_finished)
            self.proc.errorOccurred.connect(self._on_proc_error)
            self._set_running(True)
            self.status.setText("Reparation : ecriture…" if apply else "Reparation : analyse…")
            self.proc.setProgram(program); self.proc.setArguments(args); self.proc.start()

        def _on_repair_finished(self, code, _st):
            text = self.log.toPlainText()
            self.proc = None
            self._set_running(False)
            if self.repair_apply:
                self.status.setText("Reparation terminee (code 0)." if code == 0
                                    else "Reparation : erreur (code %s) — voir Journal." % code)
                self._cleanup_tmp()
                return
            if code != 0:
                self.status.setText("Reparation : erreur (voir Journal)."); self._cleanup_tmp(); return
            m = re.search(r"A_TRAITER:\s*(\d+)", text)
            n = int(m.group(1)) if m else 0
            if n == 0:
                QMessageBox.information(self, "Reparation",
                                        "Aucun texte arabe deforme (UTF-8) trouve.")
                self.status.setText("Reparation : rien a corriger."); self._cleanup_tmp(); return
            ok = QMessageBox.question(
                self, "Confirmer la reparation",
                "%d texte(s) arabe(s) deforme(s) detecte(s).\n\n"
                "Les corriger sur place (ré-encodage en WIN1256) ? Les prix, le stock "
                "et les bons ne sont PAS touches.\n\nSauvegardez la base au prealable." % n,
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No)
            if ok == QMessageBox.StandardButton.Yes:
                self._start_repair(apply=True)
            else:
                self.status.setText("Reparation annulee."); self._cleanup_tmp()

        def _cleanup_tmp(self):
            for p in (self.tmp_config_path, self.tmp_lines_path):
                if p and os.path.isfile(p):
                    try: os.remove(p)
                    except OSError: pass
            self.tmp_config_path = None
            self.tmp_lines_path = None

        def _reload_preview(self):
            path = self.excel_edit.text().strip()
            if path and os.path.isfile(path):
                self.load_preview(path)

        def _set_running(self, running):
            self.busy.setRange(0, 0) if running else self.busy.setRange(0, 1)
            if not running:
                self.busy.setValue(0)
            for b in (self.btn_preview, self.btn_import, self.btn_test, self.btn_load_lists,
                      self.btn_match, self.btn_clean, self.btn_repair, self.btn_template,
                      self.btn_sync_bc, self.btn_apply_bc, self.adv):
                b.setEnabled(not running)
            self.btn_cancel.setEnabled(running)

        def _reset_summary(self, dry):
            self.summary_state.setText("Apercu en cours…" if dry else "Import en cours…")
            self.summary_state.setStyleSheet("color:#555;")
            for v in self.sum_fields.values():
                v.setText("—")

        def _parse_summary(self, text, code):
            def grab(pat):
                m = re.search(pat, text); return m.group(1).strip() if m else None
            nopiece = grab(r"NOPIECE=(\S+)")
            ref_piece = grab(r"REF_PIECE=(\S+)")
            type_piece = grab(r"\(type\s+([^)]+)\)")
            four = grab(r"Fournisseur\s*:\s*(.+?)\s{2,}Depot")
            depot = grab(r"Depot\s*:\s*(.+)")
            for key, pat in (("lus",      r"Lignes lues dans l'Excel\s*:\s*(\d+)"),
                             ("crees",    r"Articles crees\s*:\s*(\d+)"),
                             ("existants",r"Articles existants\s*:\s*(\d+)"),
                             ("maj",      r"Articles mis a jour \(prix achat\)\s*:\s*(\d+)"),
                             ("barcodes", r"Codes-barres renseignes\s*:\s*(\d+)"),
                             ("ht",       r"Total HT\s*:\s*([\d.,]+)"),
                             ("tva",      r"Total TVA\s*:\s*([\d.,]+)"),
                             ("ttc",      r"Total TTC\s*:\s*([\d.,]+)")):
                val = grab(pat)
                if val is not None:
                    self.sum_fields[key].setText(val)
            if nopiece:
                self.sum_fields["piece"].setText(
                    "NOPIECE %s · REF_PIECE %s · type %s" % (nopiece, ref_piece or "?", type_piece or "?"))
                # Memoiser pour "Annuler le dernier import"
                self._last_nopiece = nopiece
            self.sum_fields["tiers"].setText("%s  /  %s" % (four or "(aucun)", depot or "(aucun)"))

            committed = "Import termine" in text and "commit" in text.lower()
            rolled = "DRY-RUN" in text or "rollback" in text.lower()
            if code != 0:
                self.summary_state.setText("Echec — aucune ecriture. %s" % friendly_error(text))
                self.summary_state.setStyleSheet("color:#7a1f1f;")
            elif self.current_mode == "real" and committed:
                self.summary_state.setText("Import enregistre (commit)")
                self.summary_state.setStyleSheet("color:#1f7a33;")
            elif rolled:
                self.summary_state.setText("Apercu (dry-run) — rien n'a ete ecrit (rollback)")
                self.summary_state.setStyleSheet("color:#235;")
            else:
                self.summary_state.setText("Termine"); self.summary_state.setStyleSheet("color:#555;")

    return MainWindow


def build_gui():
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication(sys.argv)
    win = _window_class()()
    win.show()
    return app.exec()


def main():
    return build_gui()


if __name__ == "__main__":
    if "--run-cli" in sys.argv:
        rest = [a for a in sys.argv[1:] if a != "--run-cli"]
        sys.exit(run_cli(rest))
    if "--run-clean" in sys.argv:
        rest = [a for a in sys.argv[1:] if a != "--run-clean"]
        sys.exit(run_clean(rest))
    if "--run-repair" in sys.argv:
        rest = [a for a in sys.argv[1:] if a != "--run-repair"]
        sys.exit(run_repair(rest))
    sys.exit(main())
