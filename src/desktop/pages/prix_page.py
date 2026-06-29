"""Page Éditeur de prix : sélection article + modification pour 1 ou 3 magasins."""

from __future__ import annotations

import sqlite3
import sys
import os
import traceback
from typing import NamedTuple

from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtWidgets import (
    QCheckBox, QDoubleSpinBox, QFormLayout, QGroupBox,
    QHBoxLayout, QLabel, QLineEdit, QMessageBox,
    QPushButton, QTableWidget, QTableWidgetItem, QTextEdit,
    QVBoxLayout, QWidget,
)

from stores import StoreRegistry
from hub.central_db import enqueue_op
from hub.write_back import is_reachable, write_prices, WriteError

_VENDOR = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))), "vendor")
_EDITOR_DIR = os.path.join(_VENDOR, "editor")
if _EDITOR_DIR not in sys.path:
    sys.path.insert(0, _EDITOR_DIR)


class _WriteTask(NamedTuple):
    store_id: int
    connect_kwargs: dict
    changes: list
    online: bool


class _PriceThread(QThread):
    progress = Signal(int, bool, str)   # store_id, ok, message
    finished_all = Signal()

    def __init__(self, tasks: list[_WriteTask], db_con: sqlite3.Connection):
        super().__init__()
        self._tasks = tasks
        self._con = db_con

    def run(self) -> None:
        for task in self._tasks:
            if task.online:
                try:
                    write_prices(task.connect_kwargs, task.changes)
                    self.progress.emit(task.store_id, True,
                                       "Magasin %d : %d articles mis à jour." % (
                                           task.store_id, len(task.changes)))
                except WriteError as exc:
                    self.progress.emit(task.store_id, False, str(exc))
                except Exception as exc:  # noqa: BLE001
                    self.progress.emit(task.store_id, False, traceback.format_exc())
            else:
                enqueue_op(self._con, task.store_id, "price_update",
                           {"changes": task.changes})
                self.progress.emit(task.store_id, True,
                                   "Magasin %d hors ligne : mis en file." % task.store_id)
        self.finished_all.emit()


class PrixPage(QWidget):
    def __init__(self, registry: StoreRegistry, db_con: sqlite3.Connection, parent=None):
        super().__init__(parent)
        self._registry = registry
        self._con = db_con
        self._thread: _PriceThread | None = None

        # ── Recherche article ──
        self._search = QLineEdit()
        self._search.setPlaceholderText("Référence ou désignation…")
        self._search.returnPressed.connect(self._do_search)
        search_btn = QPushButton("Chercher")
        search_btn.clicked.connect(self._do_search)

        self._results = QTableWidget(0, 5)
        self._results.setHorizontalHeaderLabels(
            ["Référence", "Désignation", "M1 Prix HT", "M2 Prix HT", "M3 Prix HT"])
        self._results.setEditTriggers(QTableWidget.NoEditTriggers)
        self._results.setSelectionBehavior(QTableWidget.SelectRows)
        self._results.setMaximumHeight(200)

        # ── Nouveau prix ──
        self._new_price = QDoubleSpinBox()
        self._new_price.setRange(0, 9_999_999)
        self._new_price.setDecimals(2)
        self._new_price.setSuffix(" DA")

        # ── Scope magasins ──
        self._store_checks: dict[int, QCheckBox] = {}
        scope_layout = QHBoxLayout()
        for s in registry.stores:
            cb = QCheckBox(s.name)
            cb.setChecked(True)
            self._store_checks[s.id] = cb
            scope_layout.addWidget(cb)

        apply_btn = QPushButton("Appliquer le prix")
        apply_btn.clicked.connect(self._do_apply)

        self._log = QTextEdit()
        self._log.setReadOnly(True)
        self._log.setMaximumHeight(160)

        # ── Layout ──
        search_row = QHBoxLayout()
        search_row.addWidget(self._search, 1)
        search_row.addWidget(search_btn)

        form = QFormLayout()
        form.addRow("Nouveau prix HT :", self._new_price)

        scope_box = QGroupBox("Appliquer sur :")
        scope_box.setLayout(scope_layout)

        action_row = QHBoxLayout()
        action_row.addStretch()
        action_row.addWidget(apply_btn)

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("<h3>Éditeur de prix</h3>"))
        layout.addLayout(search_row)
        layout.addWidget(self._results)
        layout.addLayout(form)
        layout.addWidget(scope_box)
        layout.addLayout(action_row)
        layout.addWidget(QLabel("Journal :"))
        layout.addWidget(self._log)

    def _log_msg(self, msg: str) -> None:
        self._log.append(msg)

    def _do_search(self) -> None:
        q = "%" + self._search.text().strip() + "%"
        store_ids = [s.id for s in self._registry.stores]

        # Récupérer les articles depuis central.db (toutes les occurrences par magasin)
        rows = self._con.execute(
            "SELECT ref_art, designation, store_id, prixventeht "
            "FROM article WHERE ref_art LIKE ? OR designation LIKE ? "
            "ORDER BY ref_art, store_id LIMIT 100",
            (q, q)).fetchall()

        # Regrouper par ref_art
        by_ref: dict[str, dict] = {}
        for r in rows:
            ref = r[0]
            if ref not in by_ref:
                by_ref[ref] = {"ref": ref, "desig": r[1], "prices": {}}
            by_ref[ref]["prices"][r[2]] = r[3]

        items = list(by_ref.values())
        self._results.setRowCount(len(items))
        for i, item in enumerate(items):
            self._results.setItem(i, 0, QTableWidgetItem(item["ref"]))
            self._results.setItem(i, 1, QTableWidgetItem(item["desig"] or ""))
            for col, sid in enumerate(store_ids[:3], start=2):
                price = item["prices"].get(sid)
                cell = QTableWidgetItem("%.2f" % float(price) if price is not None else "—")
                cell.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
                self._results.setItem(i, col, cell)

    def _do_apply(self) -> None:
        sel = self._results.selectedItems()
        if not sel:
            QMessageBox.warning(self, "Aucune sélection", "Sélectionnez un article dans la liste.")
            return

        row = self._results.currentRow()
        ref_art = self._results.item(row, 0).text()
        new_ht = self._new_price.value()
        if new_ht <= 0:
            QMessageBox.warning(self, "Prix invalide", "Le prix doit être > 0.")
            return

        selected_stores = [sid for sid, cb in self._store_checks.items() if cb.isChecked()]
        if not selected_stores:
            QMessageBox.warning(self, "Scope vide", "Sélectionnez au moins un magasin.")
            return

        self._log_msg("Application du prix %.2f DA pour %s sur %d magasin(s)…" % (
            new_ht, ref_art, len(selected_stores)))

        changes = [{"ref0": ref_art, "values": {"PRIXVENTEHT": new_ht, "PRIXVENTETTC": new_ht}}]

        tasks = []
        for sid in selected_stores:
            store = self._registry.get(sid)
            kw = store.connect_kwargs()
            online = is_reachable(store.host, store.port)
            tasks.append(_WriteTask(sid, kw, changes, online))

        self._thread = _PriceThread(tasks, self._con)
        self._thread.progress.connect(self._on_progress)
        self._thread.finished_all.connect(self._on_all_done)
        self._thread.start()

    def _on_progress(self, store_id: int, ok: bool, msg: str) -> None:
        self._log_msg(("[OK] " if ok else "[ERREUR] ") + msg)

    def _on_all_done(self) -> None:
        self._log_msg("Terminé.")
