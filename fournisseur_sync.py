"""Synchro fournisseur -> magasins — à installer SUR LE POSTE DU FOURNISSEUR.

Lit les bons de livraison (BL) de la base Firebird locale du fournisseur et
envoie chaque ligne au hub (voir /api/fournisseur/lines), qui se charge de
tout le rapprochement d'articles et de l'écriture réelle côté magasin — cet
outil ne fait QUE lire son propre Firebird et poster des lignes brutes,
aucune décision métier ici (plus facile à corriger côté hub sans avoir à
redéployer un exe sur un poste qu'on ne contrôle pas directement).

Cet outil est VOLONTAIREMENT sans état local (pas de fichier "déjà envoyé") :
à chaque passage, il relit les BL des N derniers jours (lookback_days) et
renvoie TOUTES leurs lignes ; c'est le hub (fournisseur_sync_state) qui sait
déjà lesquelles sont inchangées, nouvelles, ou modifiées depuis le dernier
passage, et agit en conséquence. Un doublon d'envoi ne crée donc jamais de
doublon de réception.

Premier lancement : aucun fichier de config -> l'interface demande la
connexion Firebird + l'URL/clé du hub, teste les deux, puis propose la liste
des clients (TIERS) de ce Firebird pour que l'utilisateur choisisse lesquels
correspondent à quels magasins (envoyé une fois au hub ; modifiable ensuite
depuis le tableau de bord web, PAS depuis cet outil).

⚠ Point à vérifier sur site : la colonne ANNULEE a un sens INVERSÉ selon le
type de pièce sur au moins une installation Netfact2/PRIME déjà rencontrée
(ANNULEE=1 = pièce ACTIVE pour les bons de réception et les mouvements de
caisse, mais probablement ANNULEE=0/NULL = active pour une vente normale
comme le BL). Cet outil ne filtre donc PAS sur ANNULEE par défaut (lit tous
les BL trouvés) pour éviter de deviner dans le mauvais sens — le journal de
l'outil affiche le nombre de BL/lignes trouvés à chaque passage : si ce
nombre semble comprendre des bons annulés, ajuster `filtrer_annulee` dans la
configuration (voir le fichier fournisseur_sync_config.json).
"""

from __future__ import annotations

import datetime
import json
import logging
import os
import sys
import time

_SRC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from stores import app_dir  # noqa: E402

log = logging.getLogger("fournisseur_sync")

CONFIG_PATH = os.path.join(app_dir(), "fournisseur_sync_config.json")

DEFAULT_CONFIG = {
    "firebird": {"host": "localhost", "port": 3050, "database": "",
                "user": "SYSDBA", "password": "masterkey", "charset": "WIN1256"},
    "hub_url": "",
    "hub_api_key": "",
    "poll_interval_seconds": 300,
    # Petite fenêtre glissante DÉLIBÉRÉMENT courte : la synchro tourne en continu
    # (chaque passage relit ces N derniers jours), donc pas besoin de remonter
    # loin pour ne rien manquer — et une valeur haute par défaut risquerait de
    # renvoyer, dès le tout premier lancement, des BL déjà saisis à la main
    # AVANT que cet outil n'existe (voir reconcile_fournisseur.py pour
    # vérifier manuellement jusqu'où c'est déjà saisi, et --backlog-days /
    # le bouton "Importer un historique…" pour un rattrapage volontaire
    # au-delà de cette fenêtre, une seule fois).
    "lookback_days": 3,
    "code_type_piece_bl": "PC_VE_B",
    # cf. l'avertissement ANNULEE dans le docstring du module.
    "filtrer_annulee": False,
}


def load_config() -> dict | None:
    if not os.path.isfile(CONFIG_PATH):
        return None
    with open(CONFIG_PATH, "r", encoding="utf-8") as fh:
        cfg = json.load(fh)
    merged = json.loads(json.dumps(DEFAULT_CONFIG))
    merged.update(cfg)
    merged["firebird"] = {**DEFAULT_CONFIG["firebird"], **(cfg.get("firebird") or {})}
    return merged


def save_config(cfg: dict) -> None:
    tmp = CONFIG_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(cfg, fh, ensure_ascii=False, indent=2)
    os.replace(tmp, CONFIG_PATH)


# --------------------------------------------------------------------------- #
#  Firebird (poste du fournisseur)
# --------------------------------------------------------------------------- #
def fb_connect(fb_cfg: dict):
    import fdb  # type: ignore
    return fdb.connect(
        host=fb_cfg.get("host") or "localhost",
        port=int(fb_cfg.get("port") or 3050),
        database=fb_cfg["database"],
        user=fb_cfg.get("user") or "SYSDBA",
        password=fb_cfg.get("password") or "",
        charset=fb_cfg.get("charset") or "WIN1256",
    )


def list_tiers(con) -> list:
    """Clients connus de ce Firebird — pour le sélecteur de correspondance au
    premier lancement (voir SetupDialog)."""
    cur = con.cursor()
    cur.execute("SELECT CODE_TIERS, RAISON_SOCIALE FROM TIERS ORDER BY RAISON_SOCIALE")
    return [{"code_tiers": str(r[0]).strip(), "raison_sociale": (r[1] or r[0]).strip()}
           for r in cur.fetchall() if r[0]]


def read_recent_bl(con, code_type_piece: str, lookback_days: int,
                   filtrer_annulee: bool = False) -> list:
    """Lit les bons de livraison récents et leurs lignes ; renvoie une liste
    de lignes brutes prêtes à poster au hub (une par ligne d'article)."""
    cur = con.cursor()
    cutoff = datetime.datetime.now() - datetime.timedelta(days=lookback_days)
    sql = "SELECT NOPIECE, CODE_TIERS FROM PIECE WHERE CODE_TYPE_PIECE = ? AND DATEPIECE >= ?"
    params = [code_type_piece, cutoff]
    if filtrer_annulee:
        sql += " AND (ANNULEE IS NULL OR ANNULEE = 0)"
    cur.execute(sql, params)
    pieces = cur.fetchall()

    lines = []
    item_cur = con.cursor()
    for nopiece, code_tiers in pieces:
        if not code_tiers:
            continue
        item_cur.execute(
            "SELECT i.NOITEM, i.REF_ART, i.QTE, i.PRIXHT, i.TVA, a.DESIGNATION, "
            "       (SELECT FIRST 1 CODE_BARRES FROM EQUIV_CBARRES e "
            "         WHERE e.REF_ART = i.REF_ART) AS BARCODE "
            "FROM ITEM i LEFT JOIN ARTICLE a ON a.REF_ART = i.REF_ART "
            "WHERE i.NOPIECE = ?", (nopiece,))
        for noitem, ref_art, qte, prixht, _tva, designation, barcode in item_cur.fetchall():
            if not ref_art:
                continue
            lines.append({
                "src_nopiece": str(nopiece), "src_noitem": str(noitem),
                "code_tiers": str(code_tiers).strip(),
                "ref_art": str(ref_art).strip(),
                "designation": (designation or "").strip() or None,
                "qte": float(qte or 0), "prix": float(prixht or 0),
                # Toujours 0, jamais lu depuis ITEM.TVA du fournisseur : c'est
                # la même entreprise qui se vend à elle-même (transfert entre
                # ses propres magasins), pas un achat externe soumis à TVA.
                "tva": 0,
                "code_barres": (str(barcode).strip() if barcode else None),
            })
    return lines


def read_bl_for_article(con, code_type_piece: str, days: int, ref: str | None,
                        designation: str | None, filtrer_annulee: bool = False) -> list:
    """Comme read_recent_bl, mais filtré sur UN article (référence exacte et/ou
    sous-chaîne de désignation) au lieu de tout lire — pour l'onglet
    « Vérification article » (diagnostic manuel, jamais utilisé par la
    synchro automatique elle-même)."""
    art_clauses, art_params = [], []
    if ref:
        art_clauses.append("i.REF_ART = ?")
        art_params.append(ref)
    if designation:
        art_clauses.append("a.DESIGNATION CONTAINING ?")
        art_params.append(designation)
    if not art_clauses:
        return []
    cutoff = datetime.datetime.now() - datetime.timedelta(days=days)
    sql = (
        "SELECT p.NOPIECE, p.DATEPIECE, p.CODE_TIERS, i.NOITEM, i.REF_ART, "
        "       a.DESIGNATION, i.QTE, i.PRIXHT "
        "FROM PIECE p JOIN ITEM i ON i.NOPIECE = p.NOPIECE "
        "LEFT JOIN ARTICLE a ON a.REF_ART = i.REF_ART "
        "WHERE p.CODE_TYPE_PIECE = ? AND p.DATEPIECE >= ? AND (%s)" % " OR ".join(art_clauses)
    )
    params = [code_type_piece, cutoff] + art_params
    if filtrer_annulee:
        sql += " AND (p.ANNULEE IS NULL OR p.ANNULEE = 0)"
    sql += " ORDER BY p.DATEPIECE"
    cur = con.cursor()
    cur.execute(sql, params)
    lines = []
    for nopiece, date, code_tiers, noitem, ref_art, desig, qte, prixht in cur.fetchall():
        if not code_tiers or not ref_art:
            continue
        lines.append({
            "src_nopiece": str(nopiece), "src_noitem": str(noitem),
            "date": str(date)[:10], "code_tiers": str(code_tiers).strip(),
            "ref_art": str(ref_art).strip(), "designation": (desig or "").strip() or None,
            "qte": float(qte or 0), "prix": float(prixht or 0),
        })
    return lines


# --------------------------------------------------------------------------- #
#  Hub (HTTP)
# --------------------------------------------------------------------------- #
class HubClient:
    def __init__(self, base_url: str, api_key: str, timeout: float = 30.0,
                lines_timeout: float = 180.0):
        self.base = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout = timeout
        # /api/fournisseur/lines peut écrire une ligne à la fois, en direct,
        # sur un magasin joignable (submit_op synchrone) — un lot de lignes
        # peut donc légitimement prendre bien plus que le timeout "rapide"
        # utilisé pour ping/stores/mapping. Distinct du timeout général : on
        # ne veut pas attendre 180s sur un simple ping qui ne répond pas.
        self.lines_timeout = lines_timeout

    def _headers(self) -> dict:
        return {"X-Api-Key": self.api_key} if self.api_key else {}

    def ping(self) -> bool:
        import requests
        try:
            r = requests.get(self.base + "/api/ping", headers=self._headers(),
                             timeout=self.timeout)
            return r.status_code == 200
        except requests.RequestException:
            return False

    def stores(self) -> list:
        import requests
        r = requests.get(self.base + "/api/client/config", headers=self._headers(),
                         timeout=self.timeout)
        r.raise_for_status()
        return r.json().get("stores", [])

    def post_mapping(self, entries: list) -> dict:
        import requests
        r = requests.post(self.base + "/api/fournisseur/mapping",
                          json={"mapping": entries}, headers=self._headers(),
                          timeout=self.timeout)
        r.raise_for_status()
        return r.json()

    def get_mapping(self) -> dict:
        import requests
        r = requests.get(self.base + "/api/fournisseur/mapping", headers=self._headers(),
                         timeout=self.timeout)
        r.raise_for_status()
        return r.json().get("mapping", {})

    def post_lines(self, lines: list, batch_size: int = 50) -> list:
        import requests
        results = []
        for i in range(0, len(lines), batch_size):
            batch = lines[i:i + batch_size]
            r = requests.post(self.base + "/api/fournisseur/lines",
                              json={"lines": batch}, headers=self._headers(),
                              timeout=self.lines_timeout)
            r.raise_for_status()
            results.extend(r.json().get("results", []))
        return results

    def reconcile(self, ref: str | None, designation: str | None, days: int,
                  father_lines: list) -> list:
        import requests
        r = requests.post(self.base + "/api/fournisseur/reconcile",
                          json={"ref": ref, "designation": designation, "days": days,
                                "father_lines": father_lines},
                          headers=self._headers(), timeout=self.lines_timeout)
        r.raise_for_status()
        return r.json().get("report", [])


# --------------------------------------------------------------------------- #
#  Un cycle de synchro (utilisé par le GUI ET par --once)
# --------------------------------------------------------------------------- #
_STATUS_LABELS = {
    "applied": "appliqué", "queued": "en file (magasin hors ligne)",
    "pending": "à valider (rapprochement par nom — tableau de bord)",
    "pending_creation": "en attente (création encore en file)",
    "unchanged": "inchangé", "skipped": "ignoré (code client non mappé)",
    "error": "ERREUR",
}


def _describe_result(line: dict, result: dict, store_names: dict) -> str:
    """Une ligne de journal lisible par ligne de BL traitée — sans ça, l'outil
    tournait « à l'aveugle » (aucun détail, juste un total en fin de passage)."""
    store_id = result.get("store_id")
    store = store_names.get(store_id, "magasin %s" % store_id) if store_id else "?"
    status = result.get("status", "?")
    label = _STATUS_LABELS.get(status, status)
    txt = "  [%s] %s « %s » x%s -> %s" % (
        store, line.get("ref_art"), line.get("designation") or line.get("ref_art"),
        line.get("qte"), label)
    if status == "error":
        txt += " (%s)" % result.get("error")
    elif status == "skipped":
        txt += " (code_tiers=%s)" % line.get("code_tiers")
    return txt


def run_cycle(cfg: dict, log_fn=log.info) -> dict:
    con = fb_connect(cfg["firebird"])
    try:
        lines = read_recent_bl(con, cfg["code_type_piece_bl"], cfg["lookback_days"],
                               cfg.get("filtrer_annulee", False))
    finally:
        con.close()
    log_fn("%d ligne(s) de BL trouvée(s) sur les %d derniers jours."
          % (len(lines), cfg["lookback_days"]))
    if not lines:
        return {"lines": 0, "applied": 0, "queued": 0, "pending": 0,
               "unchanged": 0, "skipped": 0, "errors": 0}

    client = HubClient(cfg["hub_url"], cfg["hub_api_key"])
    try:
        store_names = {s["id"]: s["name"] for s in client.stores()}
    except Exception:  # noqa: BLE001
        store_names = {}  # pas bloquant : le détail affichera juste "magasin <id>"
    results = client.post_lines(lines)
    tally = {"lines": len(lines), "applied": 0, "queued": 0, "pending": 0,
            "unchanged": 0, "skipped": 0, "errors": 0}
    for line, r in zip(lines, results):
        log_fn(_describe_result(line, r, store_names))
        status = r.get("status")
        if status in tally:
            tally[status] += 1
        elif status == "pending_creation":
            tally["pending"] += 1
        else:
            tally["errors"] += 1
    log_fn("Résultat : %d appliquée(s), %d en file, %d à valider, "
          "%d inchangée(s), %d ignorée(s), %d erreur(s)."
          % (tally["applied"], tally["queued"], tally["pending"],
             tally["unchanged"], tally["skipped"], tally["errors"]))
    return tally


# --------------------------------------------------------------------------- #
#  Vérification article (onglet dédié — diagnostic manuel, lecture seule des
#  DEUX côtés, jamais utilisé par la synchro automatique elle-même) :
#  compare, pour un article recherché, la quantité livrée par le père à
#  chaque magasin (via fournisseur_mapping) à ce que ce magasin a DÉJÀ reçu
#  sur la même période, et signale si sa référence diffère de celle du père.
# --------------------------------------------------------------------------- #
def run_reconcile(cfg: dict, ref: str | None, designation: str | None, days: int) -> list:
    con = fb_connect(cfg["firebird"])
    try:
        father_lines = read_bl_for_article(con, cfg["code_type_piece_bl"], days,
                                           ref, designation, cfg.get("filtrer_annulee", False))
    finally:
        con.close()
    client = HubClient(cfg["hub_url"], cfg["hub_api_key"])
    return client.reconcile(ref, designation, days, father_lines)


# --------------------------------------------------------------------------- #
#  Interface graphique
# --------------------------------------------------------------------------- #
def run_gui() -> None:
    from PySide6.QtCore import QTimer, QThread, Signal
    from PySide6.QtGui import QAction
    from PySide6.QtWidgets import (
        QApplication, QWidget, QDialog, QVBoxLayout, QHBoxLayout, QFormLayout, QGroupBox,
        QLabel, QLineEdit, QPushButton, QTextEdit, QSpinBox, QMessageBox,
        QSystemTrayIcon, QMenu, QTableWidget, QTableWidgetItem, QComboBox,
        QCheckBox, QFileDialog, QStyle, QInputDialog, QTabWidget, QHeaderView,
    )

    class SyncThread(QThread):
        done = Signal(dict)
        failed = Signal(str)
        logged = Signal(str)

        def __init__(self, cfg: dict):
            super().__init__()
            self._cfg = cfg

        def run(self) -> None:
            try:
                tally = run_cycle(self._cfg, log_fn=lambda m: self.logged.emit(m))
                self.done.emit(tally)
            except Exception as exc:  # noqa: BLE001
                self.failed.emit(str(exc))

    class ReconcileThread(QThread):
        done = Signal(list)
        failed = Signal(str)

        def __init__(self, cfg: dict, ref: str | None, designation: str | None, days: int):
            super().__init__()
            self._cfg, self._ref, self._designation, self._days = cfg, ref, designation, days

        def run(self) -> None:
            try:
                report = run_reconcile(self._cfg, self._ref, self._designation, self._days)
                self.done.emit(report)
            except Exception as exc:  # noqa: BLE001
                self.failed.emit(str(exc))

    class MainWindow(QWidget):
        def __init__(self):
            super().__init__()
            self.setWindowTitle("PrimeNF — Synchro fournisseur")
            self.resize(640, 560)
            self._cfg = load_config() or json.loads(json.dumps(DEFAULT_CONFIG))
            self._thread: SyncThread | None = None
            self._reconcile_thread: ReconcileThread | None = None

            # -- Connexion Firebird --
            fb = self._cfg["firebird"]
            self._fb_host = QLineEdit(fb.get("host", "localhost"))
            self._fb_port = QSpinBox(); self._fb_port.setRange(1, 65535)
            self._fb_port.setValue(int(fb.get("port", 3050)))
            self._fb_db = QLineEdit(fb.get("database", ""))
            browse_btn = QPushButton("…")
            browse_btn.setMaximumWidth(32)
            browse_btn.clicked.connect(self._browse_db)
            self._fb_user = QLineEdit(fb.get("user", "SYSDBA"))
            self._fb_pass = QLineEdit(fb.get("password", "masterkey"))
            self._fb_pass.setEchoMode(QLineEdit.Password)
            self._fb_charset = QLineEdit(fb.get("charset", "WIN1256"))
            test_fb_btn = QPushButton("Tester la connexion Firebird")
            test_fb_btn.clicked.connect(self._test_firebird)

            fb_form = QFormLayout()
            fb_form.addRow("Hôte", self._fb_host)
            fb_form.addRow("Port", self._fb_port)
            db_row = QHBoxLayout(); db_row.addWidget(self._fb_db); db_row.addWidget(browse_btn)
            fb_form.addRow("Base (.FDB)", db_row)
            fb_form.addRow("Utilisateur", self._fb_user)
            fb_form.addRow("Mot de passe", self._fb_pass)
            fb_form.addRow("Charset", self._fb_charset)
            fb_form.addRow(test_fb_btn)
            fb_box = QGroupBox("Base Firebird locale (ce poste)")
            fb_box.setLayout(fb_form)

            # -- Connexion hub --
            self._hub_url = QLineEdit(self._cfg.get("hub_url", ""))
            self._hub_url.setPlaceholderText("http://100.x.y.z:5000")
            self._hub_key = QLineEdit(self._cfg.get("hub_api_key", ""))
            self._hub_key.setEchoMode(QLineEdit.Password)
            test_hub_btn = QPushButton("Tester la connexion au hub")
            test_hub_btn.clicked.connect(self._test_hub)

            hub_form = QFormLayout()
            hub_form.addRow("URL du hub", self._hub_url)
            hub_form.addRow("Clé API", self._hub_key)
            hub_form.addRow(test_hub_btn)
            hub_box = QGroupBox("Hub (magasin principal)")
            hub_box.setLayout(hub_form)

            # -- Options --
            self._interval = QSpinBox(); self._interval.setRange(60, 3600 * 6)
            self._interval.setSuffix(" s")
            self._interval.setValue(int(self._cfg.get("poll_interval_seconds", 300)))
            self._lookback = QSpinBox(); self._lookback.setRange(1, 3650)
            self._lookback.setSuffix(" jours")
            self._lookback.setValue(int(self._cfg.get("lookback_days", 60)))
            self._filtrer_annulee = QCheckBox("Ignorer les bons marqués ANNULEE (à activer "
                                              "seulement si le journal montre des doublons "
                                              "de bons déjà annulés côté fournisseur)")
            self._filtrer_annulee.setChecked(bool(self._cfg.get("filtrer_annulee", False)))
            opt_form = QFormLayout()
            opt_form.addRow("Intervalle de synchro auto", self._interval)
            opt_form.addRow("Historique relu à chaque passage", self._lookback)
            opt_form.addRow(self._filtrer_annulee)
            opt_box = QGroupBox("Options")
            opt_box.setLayout(opt_form)

            save_btn = QPushButton("Enregistrer les paramètres")
            save_btn.clicked.connect(self._save)
            tiers_btn = QPushButton("Correspondance clients → magasins…")
            tiers_btn.clicked.connect(self._open_tiers_dialog)
            self._backlog_btn = QPushButton("Importer un historique…")
            self._backlog_btn.clicked.connect(self._import_backlog)
            top_row = QHBoxLayout()
            top_row.addWidget(save_btn)
            top_row.addWidget(tiers_btn)
            top_row.addWidget(self._backlog_btn)

            self._sync_btn = QPushButton("Synchroniser maintenant")
            self._sync_btn.clicked.connect(self._sync_now)
            self._status = QLabel("Jamais synchronisé.")
            self._log = QTextEdit(); self._log.setReadOnly(True)

            sync_tab = QWidget()
            sync_layout = QVBoxLayout(sync_tab)
            sync_layout.addWidget(fb_box)
            sync_layout.addWidget(hub_box)
            sync_layout.addWidget(opt_box)
            sync_layout.addLayout(top_row)
            sync_layout.addWidget(self._sync_btn)
            sync_layout.addWidget(self._status)
            sync_layout.addWidget(QLabel("Journal (détail ligne par ligne à chaque passage) :"))
            sync_layout.addWidget(self._log)

            # -- Onglet Vérification article --
            self._verif_ref = QLineEdit()
            self._verif_ref.setPlaceholderText("ex. 70010 (référence exacte, optionnel)")
            self._verif_desig = QLineEdit()
            self._verif_desig.setPlaceholderText("ex. recharge marqueur (sous-chaîne, optionnel)")
            self._verif_days = QSpinBox(); self._verif_days.setRange(1, 3650)
            self._verif_days.setSuffix(" jours"); self._verif_days.setValue(60)
            verif_search_btn = QPushButton("Rechercher")
            verif_search_btn.clicked.connect(self._search_reconcile)
            verif_form = QFormLayout()
            verif_form.addRow("Référence article", self._verif_ref)
            verif_form.addRow("Ou désignation (contient)", self._verif_desig)
            verif_form.addRow("Période", self._verif_days)
            verif_form.addRow(verif_search_btn)

            self._verif_table = QTableWidget(0, 6)
            self._verif_table.setHorizontalHeaderLabels([
                "Magasin", "Qté livrée (père)", "Qté déjà reçue (magasin)",
                "Réf. chez le père", "Correspondance côté magasin", "État"])
            self._verif_table.setEditTriggers(QTableWidget.NoEditTriggers)
            self._verif_table.horizontalHeader().setSectionResizeMode(
                QHeaderView.ResizeMode.Stretch)
            self._verif_status = QLabel(
                "Recherchez un article par référence et/ou désignation : compare, pour chaque "
                "magasin, ce que le père a livré à ce qu'il a déjà reçu sur la même période, et "
                "signale si la référence diffère entre les deux bases.")
            self._verif_status.setWordWrap(True)

            verif_tab = QWidget()
            verif_layout = QVBoxLayout(verif_tab)
            verif_layout.addLayout(verif_form)
            verif_layout.addWidget(self._verif_status)
            verif_layout.addWidget(self._verif_table)

            tabs = QTabWidget()
            tabs.addTab(sync_tab, "Synchro")
            tabs.addTab(verif_tab, "Vérification article")

            layout = QVBoxLayout(self)
            layout.addWidget(QLabel("<h3>Synchro fournisseur → magasins</h3>"))
            layout.addWidget(tabs)

            # QSystemTrayIcon reste invisible sans icone (Qt refuse de l'afficher,
            # avertissement silencieux "No Icon set") : sans elle, closeEvent
            # cache la fenetre dans une icone que l'utilisateur ne peut jamais
            # rouvrir, sinon en tuant le processus. Icone standard Qt (aucun
            # fichier a empaqueter avec PyInstaller).
            icon = self.style().standardIcon(QStyle.SP_ComputerIcon)
            self.setWindowIcon(icon)
            self._tray = QSystemTrayIcon(icon, self)
            self._tray.setToolTip("Synchro fournisseur")
            menu = QMenu()
            show_action = QAction("Afficher", self)
            show_action.triggered.connect(self._show_from_tray)
            sync_action = QAction("Synchroniser maintenant", self)
            sync_action.triggered.connect(self._sync_now)
            quit_action = QAction("Quitter", self)
            quit_action.triggered.connect(QApplication.instance().quit)
            menu.addAction(show_action)
            menu.addAction(sync_action)
            menu.addSeparator()
            menu.addAction(quit_action)
            self._tray.setContextMenu(menu)
            self._tray.activated.connect(
                lambda reason: self._show_from_tray()
                if reason == QSystemTrayIcon.DoubleClick else None)
            self._tray.show()

            self._timer = QTimer(self)
            self._timer.timeout.connect(self._sync_now)
            self._apply_interval()

            if not load_config():
                self._log_msg("Premier lancement : renseignez la connexion Firebird et le "
                              "hub, testez les deux, puis cliquez sur « Correspondance "
                              "clients → magasins » avant d'enregistrer.")

        # -- config --
        def _current_cfg(self) -> dict:
            return {
                "firebird": {
                    "host": self._fb_host.text().strip() or "localhost",
                    "port": self._fb_port.value(),
                    "database": self._fb_db.text().strip(),
                    "user": self._fb_user.text().strip() or "SYSDBA",
                    "password": self._fb_pass.text(),
                    "charset": self._fb_charset.text().strip() or "WIN1256",
                },
                "hub_url": self._hub_url.text().strip(),
                "hub_api_key": self._hub_key.text().strip(),
                "poll_interval_seconds": self._interval.value(),
                "lookback_days": self._lookback.value(),
                "code_type_piece_bl": self._cfg.get("code_type_piece_bl", "PC_VE_B"),
                "filtrer_annulee": self._filtrer_annulee.isChecked(),
            }

        def _apply_interval(self) -> None:
            self._timer.start(self._interval.value() * 1000)

        def _log_msg(self, msg: str) -> None:
            ts = datetime.datetime.now().strftime("%H:%M:%S")
            self._log.append("[%s] %s" % (ts, msg))

        def _browse_db(self) -> None:
            path, _ = QFileDialog.getOpenFileName(self, "Base Firebird", "",
                                                  "Firebird (*.fdb *.gdb);;Tous (*.*)")
            if path:
                self._fb_db.setText(path)

        def _test_firebird(self) -> None:
            try:
                con = fb_connect(self._current_cfg()["firebird"])
                tiers = list_tiers(con)
                con.close()
                QMessageBox.information(self, "Connexion réussie",
                                       "Connexion Firebird OK — %d client(s) trouvé(s)."
                                       % len(tiers))
            except Exception as exc:  # noqa: BLE001
                QMessageBox.warning(self, "Échec de connexion", str(exc))

        def _test_hub(self) -> None:
            cfg = self._current_cfg()
            client = HubClient(cfg["hub_url"], cfg["hub_api_key"])
            if client.ping():
                QMessageBox.information(self, "Connexion réussie", "Le hub répond.")
            else:
                QMessageBox.warning(self, "Échec de connexion",
                                   "Le hub ne répond pas (URL/clé/réseau à vérifier).")

        def _save(self) -> None:
            cfg = self._current_cfg()
            if not cfg["firebird"]["database"]:
                QMessageBox.warning(self, "Configuration incomplète",
                                   "Indiquez le chemin de la base Firebird.")
                return
            if not cfg["hub_url"]:
                QMessageBox.warning(self, "Configuration incomplète",
                                   "Indiquez l'URL du hub.")
                return
            save_config(cfg)
            self._cfg = cfg
            self._apply_interval()
            self._log_msg("Paramètres enregistrés.")

        def _open_tiers_dialog(self) -> None:
            cfg = self._current_cfg()
            try:
                con = fb_connect(cfg["firebird"])
                tiers = list_tiers(con)
                con.close()
            except Exception as exc:  # noqa: BLE001
                QMessageBox.warning(self, "Connexion Firebird échouée", str(exc))
                return
            client = HubClient(cfg["hub_url"], cfg["hub_api_key"])
            try:
                stores = client.stores()
            except Exception as exc:  # noqa: BLE001
                QMessageBox.warning(self, "Connexion au hub échouée", str(exc))
                return
            if not stores:
                QMessageBox.warning(self, "Aucun magasin",
                                   "Le hub n'a renvoyé aucun magasin.")
                return
            try:
                current_mapping = client.get_mapping()
            except Exception:  # noqa: BLE001
                # Pas bloquant : la boîte s'ouvre quand même, juste sans
                # pré-sélection (comme avant ce correctif).
                current_mapping = {}
            dlg = TiersDialog(tiers, stores, client, current_mapping, self)
            dlg.exec()

        def _sync_now(self) -> None:
            if self._thread is not None and self._thread.isRunning():
                return
            if not load_config():
                QMessageBox.warning(self, "Configuration manquante",
                                   "Enregistrez les paramètres avant de synchroniser.")
                return
            self._start_thread(load_config(), "Synchronisation en cours…")

        def _import_backlog(self) -> None:
            if self._thread is not None and self._thread.isRunning():
                return
            cfg = load_config()
            if not cfg:
                QMessageBox.warning(self, "Configuration manquante",
                                   "Enregistrez les paramètres avant d'importer un historique.")
                return
            days, ok = QInputDialog.getInt(
                self, "Importer un historique",
                "Nombre de jours à remonter (au-delà de la fenêtre habituelle de %d jour(s)) :"
                % cfg.get("lookback_days", 3), cfg.get("lookback_days", 3), 1, 3650)
            if not ok:
                return
            answer = QMessageBox.question(
                self, "Confirmer l'import",
                "Ceci va relire et renvoyer TOUS les bons de livraison des %d derniers "
                "jours, une seule fois (sans changer vos paramètres habituels).\n\n"
                "Si des bons de cette période ont déjà été saisis à la main dans un "
                "magasin, ça créera des doublons — vérifiez d'abord avec "
                "reconcile_fournisseur.py si vous n'êtes pas sûr. Continuer ?" % days)
            if answer != QMessageBox.Yes:
                return
            self._start_thread(dict(cfg, lookback_days=days),
                              "Import de l'historique (%d jours) en cours…" % days)

        def _start_thread(self, cfg: dict, status_text: str) -> None:
            self._sync_btn.setEnabled(False)
            self._backlog_btn.setEnabled(False)
            self._status.setText(status_text)
            self._thread = SyncThread(cfg)
            self._thread.logged.connect(self._log_msg)
            self._thread.done.connect(self._sync_done)
            self._thread.failed.connect(self._sync_failed)
            self._thread.start()

        def _sync_done(self, tally: dict) -> None:
            self._sync_btn.setEnabled(True)
            self._backlog_btn.setEnabled(True)
            ts = datetime.datetime.now().strftime("%d/%m/%Y %H:%M:%S")
            self._status.setText("Dernière synchro : %s — %d ligne(s)" % (ts, tally["lines"]))

        def _sync_failed(self, msg: str) -> None:
            self._sync_btn.setEnabled(True)
            self._backlog_btn.setEnabled(True)
            self._status.setText("Échec de la dernière synchro.")
            self._log_msg("ÉCHEC : %s" % msg)

        def _search_reconcile(self) -> None:
            if self._reconcile_thread is not None and self._reconcile_thread.isRunning():
                return
            ref = self._verif_ref.text().strip() or None
            designation = self._verif_desig.text().strip() or None
            if not ref and not designation:
                QMessageBox.warning(self, "Filtre manquant",
                                   "Indiquez une référence et/ou une désignation à rechercher.")
                return
            cfg = self._current_cfg()
            if not cfg["firebird"]["database"] or not cfg["hub_url"]:
                QMessageBox.warning(self, "Configuration incomplète",
                                   "Renseignez la connexion Firebird et le hub (testez-les si "
                                   "besoin) avant de rechercher — inutile d'enregistrer d'abord.")
                return
            self._verif_table.setRowCount(0)
            self._verif_status.setText("Recherche en cours…")
            self._reconcile_thread = ReconcileThread(cfg, ref, designation,
                                                      self._verif_days.value())
            self._reconcile_thread.done.connect(self._reconcile_done)
            self._reconcile_thread.failed.connect(self._reconcile_failed)
            self._reconcile_thread.start()

        def _reconcile_done(self, report: list) -> None:
            self._verif_table.setRowCount(len(report))
            for row, entry in enumerate(report):
                matches = entry.get("matches") or []
                m = matches[0] if matches else None
                if m is None:
                    corres = "-"
                elif m.get("status") == "exact":
                    corres = "identique (%s)" % m.get("match_ref")
                elif m.get("status") == "matched":
                    corres = "RÉF. DIFFÉRENTE : %s — « %s » (nom, %.0f%%)" % (
                        m.get("match_ref"), m.get("match_designation") or "",
                        (m.get("match_score") or 0) * 100)
                else:
                    corres = "aucune correspondance côté magasin"
                ref_pere = (matches[0].get("ref_art") if matches else None) or "-"
                if entry.get("online") is False:
                    etat = "hors ligne (pas de vérification possible)"
                elif entry.get("match_error") or entry.get("reception_error"):
                    etat = "erreur : %s" % (entry.get("match_error") or entry.get("reception_error"))
                elif entry.get("online") is None:
                    etat = "-"
                else:
                    etat = "en ligne"
                reception = entry.get("reception_qte")
                values = [entry.get("store_name"), "%.2f" % entry.get("qte_pere", 0.0),
                         "%.2f" % reception if reception is not None else "-",
                         ref_pere, corres, etat]
                for col, val in enumerate(values):
                    self._verif_table.setItem(row, col, QTableWidgetItem(str(val)))
            self._verif_status.setText(
                "%d magasin(s). Comparez « Qté livrée » et « Qté déjà reçue » sur la période : "
                "si elles correspondent déjà, c'est probablement saisi à la main. Une ligne "
                "« RÉF. DIFFÉRENTE » signale un article connu sous une autre référence côté "
                "magasin — à vérifier avant d'activer la synchro automatique sur cet article."
                % len(report))

        def _reconcile_failed(self, msg: str) -> None:
            self._verif_status.setText("Échec de la recherche : %s" % msg)

        def _show_from_tray(self) -> None:
            self.showNormal()
            self.activateWindow()

        def closeEvent(self, event) -> None:  # noqa: N802 (Qt override)
            event.ignore()
            self.hide()
            self._tray.showMessage("Synchro fournisseur",
                                   "Toujours actif en arrière-plan.",
                                   QSystemTrayIcon.Information, 3000)

    class TiersDialog(QDialog):
        def __init__(self, tiers: list, stores: list, client: HubClient,
                    current_mapping: dict | None = None, parent=None):
            super().__init__(parent)
            self.setWindowTitle("Correspondance clients → magasins")
            self.resize(560, 480)
            self._tiers = tiers
            self._client = client
            current_mapping = current_mapping or {}

            self._table = QTableWidget(len(tiers), 3)
            self._table.setHorizontalHeaderLabels(["Code client", "Nom", "Magasin"])
            self._table.setEditTriggers(QTableWidget.NoEditTriggers)
            for i, t in enumerate(tiers):
                self._table.setItem(i, 0, QTableWidgetItem(t["code_tiers"]))
                self._table.setItem(i, 1, QTableWidgetItem(t["raison_sociale"]))
                combo = QComboBox()
                combo.addItem("— Ignorer —", None)
                for s in stores:
                    combo.addItem(s["name"], s["id"])
                # Pré-sélectionne ce qui est DÉJÀ enregistré côté hub — sinon la
                # boîte repart de « Ignorer » partout à chaque ouverture, ce qui
                # donne l'impression (à tort) que rien n'a jamais été sauvegardé.
                existing = current_mapping.get(t["code_tiers"])
                if existing:
                    idx = combo.findData(existing.get("store_id"))
                    if idx >= 0:
                        combo.setCurrentIndex(idx)
                self._table.setCellWidget(i, 2, combo)

            save_btn = QPushButton("Enregistrer sur le hub")
            save_btn.clicked.connect(self._save)

            layout = QVBoxLayout(self)
            layout.addWidget(QLabel(
                "Pour chaque client de ce Firebird, choisissez le magasin correspondant "
                "(ou « Ignorer » — ses bons de livraison ne seront jamais envoyés)."))
            layout.addWidget(self._table)
            layout.addWidget(save_btn)

        def _save(self) -> None:
            from PySide6.QtWidgets import QMessageBox
            entries = []
            for i, t in enumerate(self._tiers):
                combo = self._table.cellWidget(i, 2)
                store_id = combo.currentData()
                if store_id:
                    entries.append({"code_tiers": t["code_tiers"], "store_id": store_id,
                                   "raison_sociale": t["raison_sociale"]})
            if not entries:
                QMessageBox.warning(self, "Rien à enregistrer",
                                   "Choisissez au moins un magasin.")
                return
            try:
                self._client.post_mapping(entries)
            except Exception as exc:  # noqa: BLE001
                QMessageBox.warning(self, "Échec", str(exc))
                return
            QMessageBox.information(self, "Enregistré",
                                   "%d correspondance(s) envoyée(s) au hub." % len(entries))
            self.close()

    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)
    win = MainWindow()
    win.show()
    sys.exit(app.exec())


def main() -> None:
    import argparse
    ap = argparse.ArgumentParser(description="Synchro fournisseur -> magasins.")
    ap.add_argument("--once", action="store_true",
                    help="Une seule synchro en ligne de commande, sans interface.")
    ap.add_argument("--backlog-days", type=int, default=None,
                    help="Rattrapage PONCTUEL : relit les N derniers jours (au lieu de la "
                         "petite fenêtre glissante configurée) et les envoie une seule fois, "
                         "sans modifier la configuration. À utiliser après avoir vérifié avec "
                         "reconcile_fournisseur.py jusqu'où c'est déjà saisi à la main côté "
                         "magasin, pour rattraper ce qui ne l'est pas encore.")
    ap.add_argument("--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    args = ap.parse_args()

    # L'exe est construit --windowed (pas de fenêtre console à côté du tray,
    # cf. build_fournisseur_sync.bat) : sys.stdout/sys.stderr valent alors
    # None (comportement PyInstaller sous Windows sans console), un
    # StreamHandler planterait au premier message. Un fichier journal à côté
    # de l'exe reste donc la SEULE trace disponible en usage normal (GUI ou
    # --once/--backlog-days lancés depuis une tâche planifiée sans console) —
    # utile aussi pour diagnostiquer un passage automatique après coup.
    from logging.handlers import RotatingFileHandler
    handlers = []
    try:
        fh = RotatingFileHandler(os.path.join(app_dir(), "fournisseur_sync.log"),
                                 maxBytes=2_000_000, backupCount=3, encoding="utf-8")
        handlers.append(fh)
    except OSError:
        pass
    if sys.stderr is not None:
        handlers.append(logging.StreamHandler())
    logging.basicConfig(level=getattr(logging, args.log_level),
                        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
                        datefmt="%Y-%m-%d %H:%M:%S", handlers=handlers or None)

    if args.once or args.backlog_days is not None:
        cfg = load_config()
        if not cfg or not cfg.get("hub_url") or not cfg["firebird"].get("database"):
            sys.exit("Configuration manquante ou incomplète (%s) — lancez l'outil sans "
                     "--once une première fois pour la créer." % CONFIG_PATH)
        if args.backlog_days is not None:
            cfg = dict(cfg, lookback_days=args.backlog_days)
        run_cycle(cfg)
        return

    run_gui()


if __name__ == "__main__":
    main()
