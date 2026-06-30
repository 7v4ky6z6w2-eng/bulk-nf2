"""Page Trésorerie : encaissements du jour par magasin / mode (via le hub)."""

from __future__ import annotations

from datetime import datetime

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QHBoxLayout, QLabel, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)


class TresoreriePage(QWidget):
    def __init__(self, data, store_names: dict, parent=None):
        super().__init__(parent)
        self._data = data
        self._names = store_names

        self._day_lbl = QLabel()
        self._total_lbl = QLabel()
        self._total_lbl.setStyleSheet("font-size:18px; font-weight:bold; color:#0d6efd;")

        self._table = QTableWidget(0, 4)
        self._table.setHorizontalHeaderLabels(["Magasin", "Mode", "Total (DA)", "Nb opérations"])
        self._table.horizontalHeader().setStretchLastSection(True)
        self._table.setEditTriggers(QTableWidget.NoEditTriggers)
        self._table.setSortingEnabled(True)

        top = QHBoxLayout()
        top.addWidget(QLabel("<h3>Trésorerie —</h3>"))
        top.addWidget(self._day_lbl)
        top.addStretch()
        top.addWidget(self._total_lbl)

        layout = QVBoxLayout(self)
        layout.addLayout(top)
        layout.addWidget(self._table)

        self._timer = QTimer(self)
        self._timer.timeout.connect(self.refresh)
        self._timer.start(60_000)
        self.refresh()

    def refresh(self) -> None:
        day = datetime.now().strftime("%Y-%m-%d")
        self._day_lbl.setText("<h3>%s</h3>" % day)
        try:
            rows = self._data.tresorerie_today(day)
        except Exception:  # noqa: BLE001
            return
        grand = sum(float(r.get("total_encaisse") or 0) for r in rows)
        self._total_lbl.setText("Total : %.2f DA" % grand)

        self._table.setRowCount(len(rows))
        for i, r in enumerate(rows):
            sid = r.get("store_id")
            name = self._names.get(sid, "Magasin %s" % sid)
            self._table.setItem(i, 0, QTableWidgetItem(name))
            self._table.setItem(i, 1, QTableWidgetItem(r.get("mode_paiement") or "?"))
            amt = QTableWidgetItem("%.2f" % float(r.get("total_encaisse") or 0))
            amt.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
            self._table.setItem(i, 2, amt)
            nb = QTableWidgetItem(str(r.get("nb_transactions") or 0))
            nb.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
            self._table.setItem(i, 3, nb)
