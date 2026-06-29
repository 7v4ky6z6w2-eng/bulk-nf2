"""Page Ventes : pièces des 7 derniers jours."""

from __future__ import annotations

import sqlite3

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QHBoxLayout, QLabel, QPushButton,
    QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)


class VentesPage(QWidget):
    def __init__(self, db_con: sqlite3.Connection, store_names: dict, parent=None):
        super().__init__(parent)
        self._con = db_con
        self._names = store_names

        btn = QPushButton("Actualiser")
        btn.clicked.connect(self.refresh)

        self._table = QTableWidget(0, 6)
        self._table.setHorizontalHeaderLabels(
            ["Magasin", "Date", "N° Pièce", "Client", "TTC", "Règlement"])
        self._table.horizontalHeader().setStretchLastSection(False)
        self._table.setColumnWidth(3, 200)
        self._table.setEditTriggers(QTableWidget.NoEditTriggers)
        self._table.setSortingEnabled(True)

        top = QHBoxLayout()
        top.addWidget(QLabel("<h3>Ventes (7 derniers jours)</h3>"))
        top.addStretch()
        top.addWidget(btn)

        layout = QVBoxLayout(self)
        layout.addLayout(top)
        layout.addWidget(self._table)

        self._timer = QTimer(self)
        self._timer.timeout.connect(self.refresh)
        self._timer.start(120_000)
        self.refresh()

    def refresh(self) -> None:
        rows = self._con.execute(
            "SELECT p.store_id, p.datepiece, p.nopiece, "
            "       COALESCE(t.raison_sociale, p.code_tiers, '—'), "
            "       p.montantttc, p.code_mode_regl "
            "FROM piece p LEFT JOIN tiers t "
            "  ON p.store_id=t.store_id AND p.code_tiers=t.code_tiers "
            "WHERE p.datepiece >= date('now','-7 days') "
            "ORDER BY p.datepiece DESC LIMIT 300").fetchall()

        self._table.setRowCount(len(rows))
        for i, r in enumerate(rows):
            sid = r[0]
            name = self._names.get(sid, "M%d" % sid)
            self._table.setItem(i, 0, QTableWidgetItem(name))
            self._table.setItem(i, 1, QTableWidgetItem(str(r[1] or "")))
            self._table.setItem(i, 2, QTableWidgetItem(str(r[2] or "")))
            self._table.setItem(i, 3, QTableWidgetItem(str(r[3] or "")))
            ttc = QTableWidgetItem("%.2f" % float(r[4] or 0))
            ttc.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
            self._table.setItem(i, 4, ttc)
            self._table.setItem(i, 5, QTableWidgetItem(str(r[5] or "")))
