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
    "lookback_days": 60,
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


# --------------------------------------------------------------------------- #
#  Hub (HTTP)
# --------------------------------------------------------------------------- #
class HubClient:
    def __init__(self, base_url: str, api_key: str, timeout: float = 30.0):
        self.base = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout = timeout

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

    def post_lines(self, lines: list, batch_size: int = 200) -> list:
        import requests
        results = []
        for i in range(0, len(lines), batch_size):
            batch = lines[i:i + batch_size]
            r = requests.post(self.base + "/api/fournisseur/lines",
                              json={"lines": batch}, headers=self._headers(),
                              timeout=self.timeout)
            r.raise_for_status()
            results.extend(r.json().get("results", []))
        return results


# --------------------------------------------------------------------------- #
#  Un cycle de synchro (utilisé par le GUI ET par --once)
# --------------------------------------------------------------------------- #
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
    results = client.post_lines(lines)
    tally = {"lines": len(lines), "applied": 0, "queued": 0, "pending": 0,
            "unchanged": 0, "skipped": 0, "errors": 0}
    for r in results:
        status = r.get("status")
        if status in tally:
            tally[status] += 1
        elif status == "pending_creation":
            tally["pending"] += 1
        else:
            tally["errors"] += 1
        if status == "error":
            log_fn("Erreur sur une ligne : %s" % r.get("error"))
    log_fn("Résultat : %d appliquée(s), %d en file, %d à valider, "
          "%d inchangée(s), %d ignorée(s), %d erreur(s)."
          % (tally["applied"], tally["queued"], tally["pending"],
             tally["unchanged"], tally["skipped"], tally["errors"]))
    return tally


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
        QCheckBox, QFileDialog, QStyle,
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

    class MainWindow(QWidget):
        def __init__(self):
            super().__init__()
            self.setWindowTitle("PrimeNF — Synchro fournisseur")
            self.resize(640, 560)
            self._cfg = load_config() or json.loads(json.dumps(DEFAULT_CONFIG))
            self._thread: SyncThread | None = None

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
            top_row = QHBoxLayout()
            top_row.addWidget(save_btn)
            top_row.addWidget(tiers_btn)

            self._sync_btn = QPushButton("Synchroniser maintenant")
            self._sync_btn.clicked.connect(self._sync_now)
            self._status = QLabel("Jamais synchronisé.")
            self._log = QTextEdit(); self._log.setReadOnly(True)

            layout = QVBoxLayout(self)
            layout.addWidget(QLabel("<h3>Synchro fournisseur → magasins</h3>"))
            layout.addWidget(fb_box)
            layout.addWidget(hub_box)
            layout.addWidget(opt_box)
            layout.addLayout(top_row)
            layout.addWidget(self._sync_btn)
            layout.addWidget(self._status)
            layout.addWidget(QLabel("Journal :"))
            layout.addWidget(self._log)

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
            dlg = TiersDialog(tiers, stores, client, self)
            dlg.exec()

        def _sync_now(self) -> None:
            if self._thread is not None and self._thread.isRunning():
                return
            if not load_config():
                QMessageBox.warning(self, "Configuration manquante",
                                   "Enregistrez les paramètres avant de synchroniser.")
                return
            self._sync_btn.setEnabled(False)
            self._status.setText("Synchronisation en cours…")
            self._thread = SyncThread(load_config())
            self._thread.logged.connect(self._log_msg)
            self._thread.done.connect(self._sync_done)
            self._thread.failed.connect(self._sync_failed)
            self._thread.start()

        def _sync_done(self, tally: dict) -> None:
            self._sync_btn.setEnabled(True)
            ts = datetime.datetime.now().strftime("%d/%m/%Y %H:%M:%S")
            self._status.setText("Dernière synchro : %s — %d ligne(s)" % (ts, tally["lines"]))

        def _sync_failed(self, msg: str) -> None:
            self._sync_btn.setEnabled(True)
            self._status.setText("Échec de la dernière synchro.")
            self._log_msg("ÉCHEC : %s" % msg)

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
        def __init__(self, tiers: list, stores: list, client: HubClient, parent=None):
            super().__init__(parent)
            self.setWindowTitle("Correspondance clients → magasins")
            self.resize(560, 480)
            self._tiers = tiers
            self._client = client

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
    ap.add_argument("--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    args = ap.parse_args()

    logging.basicConfig(level=getattr(logging, args.log_level),
                        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
                        datefmt="%Y-%m-%d %H:%M:%S")

    if args.once:
        cfg = load_config()
        if not cfg or not cfg.get("hub_url") or not cfg["firebird"].get("database"):
            sys.exit("Configuration manquante ou incomplète (%s) — lancez l'outil sans "
                     "--once une première fois pour la créer." % CONFIG_PATH)
        run_cycle(cfg)
        return

    run_gui()


if __name__ == "__main__":
    main()
