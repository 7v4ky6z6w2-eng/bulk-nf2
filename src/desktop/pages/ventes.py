"""Page Ventes : pièces des 7 derniers jours (données via le hub)."""

from __future__ import annotations

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QHBoxLayout, QLabel, QPushButton,
    QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)


class VentesPage(QWidget):
    def __init__(self, data, store_names: dict, parent=None):
        super().__init__(parent)
        self._data = data
        self._names = store_names

        btn = QPushButton("Actualiser")
        btn.clicked.connect(self.refresh)

        self._table = QTableWidget(0, 6)
        self._table.setHorizontalHeaderLabels(
            ["Magasin", "Date", "N° Pièce", "Client", "TTC", "Règlement"])
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
        try:
            rows = self._data.ventes_rows()
        except Exception:  # noqa: BLE001
            return
        self._table.setRowCount(len(rows))
        for i, r in enumerate(rows):
            sid = r.get("store_id")
            name = self._names.get(sid, "M%s" % sid)
            self._table.setItem(i, 0, QTableWidgetItem(name))
            self._table.setItem(i, 1, QTableWidgetItem(str(r.get("datepiece") or "")))
            self._table.setItem(i, 2, QTableWidgetItem(str(r.get("nopiece") or "")))
            self._table.setItem(i, 3, QTableWidgetItem(str(r.get("client") or "—")))
            ttc = QTableWidgetItem("%.2f" % float(r.get("montantttc") or 0))
            ttc.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
            self._table.setItem(i, 4, ttc)
            self._table.setItem(i, 5, QTableWidgetItem(str(r.get("code_mode_regl") or "")))
