"""Page Stock : vue agrégée du stock par magasin."""

from __future__ import annotations

import sqlite3

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QHBoxLayout, QLabel, QLineEdit, QPushButton,
    QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)


class StockPage(QWidget):
    def __init__(self, db_con: sqlite3.Connection, store_names: dict, parent=None):
        super().__init__(parent)
        self._con = db_con
        self._names = store_names

        self._search = QLineEdit()
        self._search.setPlaceholderText("Rechercher par référence ou désignation…")
        self._search.returnPressed.connect(self.refresh)
        btn = QPushButton("Actualiser")
        btn.clicked.connect(self.refresh)

        self._table = QTableWidget(0, 5)
        self._table.setHorizontalHeaderLabels(["Magasin", "Référence", "Désignation", "Dépôt", "Qté"])
        self._table.horizontalHeader().setStretchLastSection(False)
        self._table.setColumnWidth(2, 260)
        self._table.setEditTriggers(QTableWidget.NoEditTriggers)
        self._table.setSortingEnabled(True)

        top = QHBoxLayout()
        top.addWidget(self._search)
        top.addWidget(btn)

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("<h3>Stock</h3>"))
        layout.addLayout(top)
        layout.addWidget(self._table)

        self._timer = QTimer(self)
        self._timer.timeout.connect(self.refresh)
        self._timer.start(120_000)
        self.refresh()

    def refresh(self) -> None:
        q = "%" + (self._search.text().strip() or "") + "%"
        rows = self._con.execute(
            "SELECT a.store_id, a.ref_art, a.designation, s.code_depot, s.qte_stock "
            "FROM stock_snapshot s JOIN article a "
            "  ON s.store_id=a.store_id AND s.ref_art=a.ref_art "
            "WHERE a.ref_art LIKE ? OR a.designation LIKE ? "
            "ORDER BY a.store_id, a.ref_art LIMIT 500",
            (q, q)).fetchall()

        self._table.setRowCount(len(rows))
        for i, r in enumerate(rows):
            sid = r["store_id"] if hasattr(r, "__getitem__") else r[0]
            name = self._names.get(sid, "M%d" % sid)
            self._table.setItem(i, 0, QTableWidgetItem(name))
            self._table.setItem(i, 1, QTableWidgetItem(str(r[1])))
            self._table.setItem(i, 2, QTableWidgetItem(str(r[2] or "")))
            self._table.setItem(i, 3, QTableWidgetItem(str(r[3] or "")))
            qty = QTableWidgetItem("%.2f" % float(r[4] or 0))
            qty.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
            if float(r[4] or 0) < 0:
                qty.setForeground(Qt.red)
            self._table.setItem(i, 4, qty)
