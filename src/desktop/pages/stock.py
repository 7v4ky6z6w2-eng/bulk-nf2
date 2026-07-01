"""Page Stock : vue agrégée du stock par magasin (données via le hub)."""

from __future__ import annotations

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QHBoxLayout, QLabel, QLineEdit, QPushButton,
    QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

from desktop.format import fmt_qty


class StockPage(QWidget):
    def __init__(self, data, store_names: dict, parent=None):
        super().__init__(parent)
        self._data = data
        self._names = store_names

        self._search = QLineEdit()
        self._search.setPlaceholderText("Rechercher par référence ou désignation…")
        self._search.returnPressed.connect(self.refresh)
        btn = QPushButton("Actualiser")
        btn.clicked.connect(self.refresh)

        self._table = QTableWidget(0, 5)
        self._table.setHorizontalHeaderLabels(["Magasin", "Référence", "Désignation", "Dépôt", "Qté"])
        self._table.setColumnWidth(2, 260)
        self._table.setEditTriggers(QTableWidget.NoEditTriggers)
        self._table.setSortingEnabled(True)
        self._table.setAlternatingRowColors(True)
        self._table.horizontalHeader().setStretchLastSection(True)

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
        try:
            rows = self._data.stock_rows(self._search.text().strip())
        except Exception:  # noqa: BLE001
            return
        self._table.setRowCount(len(rows))
        for i, r in enumerate(rows):
            sid = r.get("store_id")
            name = self._names.get(sid, "M%s" % sid)
            self._table.setItem(i, 0, QTableWidgetItem(name))
            self._table.setItem(i, 1, QTableWidgetItem(str(r.get("ref_art") or "")))
            self._table.setItem(i, 2, QTableWidgetItem(str(r.get("designation") or "")))
            self._table.setItem(i, 3, QTableWidgetItem(str(r.get("code_depot") or "")))
            qval = float(r.get("qte_stock") or 0)
            qty = QTableWidgetItem(fmt_qty(qval))
            qty.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
            if qval <= 0:   # rupture (0) ou anomalie (négatif) : en rouge
                qty.setForeground(Qt.red)
            self._table.setItem(i, 4, qty)
