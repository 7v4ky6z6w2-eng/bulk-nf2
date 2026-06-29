"""Page Import BDR : sélecteur de magasin + import direct ou en file."""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
import traceback

from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtWidgets import (
    QFileDialog, QHBoxLayout, QLabel, QMessageBox,
    QPushButton, QTextEdit, QVBoxLayout, QWidget,
)

from stores import StoreRegistry
from hub.central_db import enqueue_op
from hub.write_back import is_reachable, write_bdr, WriteError
from desktop.store_picker import StorePicker

# Vendor path pour accéder aux helpers BDR
_VENDOR = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))), "vendor")
_BDR_DIR = os.path.join(_VENDOR, "bdr")
if _BDR_DIR not in sys.path:
    sys.path.insert(0, _BDR_DIR)


class _ImportThread(QThread):
    done = Signal(bool, str)  # (ok, message)

    def __init__(self, connect_kwargs: dict, config: dict, lines: list):
        super().__init__()
        self._kw = connect_kwargs
        self._cfg = config
        self._lines = lines

    def run(self) -> None:
        try:
            write_bdr(self._kw, self._cfg, self._lines)
            self.done.emit(True, "Import BDR terminé avec succès (%d lignes)." % len(self._lines))
        except WriteError as exc:
            self.done.emit(False, str(exc))
        except Exception as exc:  # noqa: BLE001
            self.done.emit(False, traceback.format_exc())


class BdrPage(QWidget):
    def __init__(self, registry: StoreRegistry, db_con: sqlite3.Connection, parent=None):
        super().__init__(parent)
        self._registry = registry
        self._con = db_con
        self._lines: list = []
        self._config: dict = {}
        self._excel_path: str = ""
        self._thread: _ImportThread | None = None

        self._picker = StorePicker(registry)
        self._excel_btn = QPushButton("Choisir fichier Excel…")
        self._excel_lbl = QLabel("Aucun fichier sélectionné")
        self._preview_btn = QPushButton("Prévisualiser / Charger")
        self._preview_btn.setEnabled(False)
        self._import_btn = QPushButton("Importer")
        self._import_btn.setEnabled(False)
        self._log = QTextEdit()
        self._log.setReadOnly(True)
        self._log.setMaximumHeight(180)

        self._excel_btn.clicked.connect(self._choose_excel)
        self._preview_btn.clicked.connect(self._load_preview)
        self._import_btn.clicked.connect(self._do_import)

        top = QHBoxLayout()
        top.addWidget(self._picker)

        file_row = QHBoxLayout()
        file_row.addWidget(self._excel_btn)
        file_row.addWidget(self._excel_lbl, 1)
        file_row.addWidget(self._preview_btn)

        action_row = QHBoxLayout()
        action_row.addStretch()
        action_row.addWidget(self._import_btn)

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("<h3>Import Bon de Réception</h3>"))
        layout.addLayout(top)
        layout.addLayout(file_row)
        layout.addLayout(action_row)
        layout.addWidget(QLabel("Journal :"))
        layout.addWidget(self._log)

    def _log_msg(self, msg: str) -> None:
        self._log.append(msg)

    def _choose_excel(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Choisir un fichier Excel", "",
            "Excel (*.xlsx *.xls);;Tous (*)")
        if path:
            self._excel_path = path
            self._excel_lbl.setText(os.path.basename(path))
            self._preview_btn.setEnabled(True)
            self._import_btn.setEnabled(False)
            self._lines = []
            self._log_msg("Fichier : %s" % path)

    def _load_preview(self) -> None:
        """Charge l'Excel via le module BDR vendorisé et affiche un résumé."""
        try:
            import import_bon_reception as bdr  # type: ignore
            cfg = self._build_config()
            loader = bdr.ExcelLoader(self._excel_path, cfg)
            self._lines = loader.load()
            self._config = cfg
            self._log_msg("Chargé : %d articles" % len(self._lines))
            self._import_btn.setEnabled(bool(self._lines))
        except Exception as exc:  # noqa: BLE001
            self._log_msg("Erreur chargement : %s" % exc)
            self._import_btn.setEnabled(False)

    def _build_config(self) -> dict:
        sid = self._picker.current_id() or 0
        store = self._registry.get(sid)
        kw = store.connect_kwargs()
        return {
            "host": kw["host"],
            "port": kw["port"],
            "database": kw["database"],
            "user": kw["user"],
            "password": kw["password"],
            "charset": kw["charset"],
            "type_piece": "PC_AC_B",
        }

    def _do_import(self) -> None:
        if not self._lines:
            return
        sid = self._picker.current_id() or 0
        store = self._registry.get(sid)
        kw = store.connect_kwargs()
        online = is_reachable(store.host, store.port)

        if online:
            self._log_msg("Magasin EN LIGNE — import direct…")
            self._import_btn.setEnabled(False)
            self._thread = _ImportThread(kw, self._config, self._lines)
            self._thread.done.connect(self._on_done)
            self._thread.start()
        else:
            # Mise en file
            enqueue_op(self._con, sid, "bdr_import",
                       {"config": self._config, "lines": self._lines})
            self._log_msg(
                "Magasin HORS LIGNE — opération mise en file.\n"
                "Elle sera appliquée automatiquement à la prochaine connexion du magasin.")
            QMessageBox.information(self, "En file",
                "BDR mis en file d'attente pour %s.\n"
                "Une notification Telegram/ntfy sera envoyée à l'application." % store.name)

    def _on_done(self, ok: bool, msg: str) -> None:
        self._import_btn.setEnabled(True)
        self._log_msg(msg)
        if ok:
            QMessageBox.information(self, "Succès", msg)
        else:
            QMessageBox.critical(self, "Échec", msg)
