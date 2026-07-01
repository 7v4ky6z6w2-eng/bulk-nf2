"""Page Trésorerie : entrées / sorties du jour par magasin, mode et caisse.

Netfact2 sépare le cash flow par caisse (caisse1, caisse2…) et par sens :
entrée (recette) / sortie (dépense). On affiche les deux totaux séparément
plus le solde (entrées − sorties). Menu « Caisse » = une caisse précise ou
« Globale » (toutes cumulées).
"""

from __future__ import annotations

from datetime import datetime

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QComboBox, QHBoxLayout, QLabel, QTableWidget, QTableWidgetItem,
    QVBoxLayout, QWidget,
)

from desktop.format import fmt_da

_GLOBALE = "Globale (toutes caisses)"


class TresoreriePage(QWidget):
    def __init__(self, data, store_names: dict, parent=None):
        super().__init__(parent)
        self._data = data
        self._names = store_names

        self._day_lbl = QLabel()
        self._entree_lbl = QLabel(); self._entree_lbl.setStyleSheet("font-weight:bold; color:#28a745;")
        self._sortie_lbl = QLabel(); self._sortie_lbl.setStyleSheet("font-weight:bold; color:#dc3545;")
        self._solde_lbl = QLabel(); self._solde_lbl.setStyleSheet("font-size:16px; font-weight:bold; color:#0d6efd;")

        self._caisse = QComboBox()
        self._caisse.addItem(_GLOBALE)
        self._caisse.currentIndexChanged.connect(self.refresh)
        self._caisses_loaded = False

        self._table = QTableWidget(0, 4)
        self._table.setHorizontalHeaderLabels(
            ["Magasin", "Mode", "Entrées (DA)", "Sorties (DA)"])
        self._table.horizontalHeader().setStretchLastSection(True)
        self._table.setEditTriggers(QTableWidget.NoEditTriggers)
        self._table.setSortingEnabled(True)

        top = QHBoxLayout()
        top.addWidget(QLabel("<h3>Trésorerie —</h3>"))
        top.addWidget(self._day_lbl)
        top.addSpacing(16)
        top.addWidget(QLabel("Caisse :"))
        top.addWidget(self._caisse)
        top.addStretch()

        totals = QHBoxLayout()
        totals.addWidget(QLabel("Entrées :")); totals.addWidget(self._entree_lbl)
        totals.addSpacing(24)
        totals.addWidget(QLabel("Sorties :")); totals.addWidget(self._sortie_lbl)
        totals.addSpacing(24)
        totals.addWidget(QLabel("Solde :")); totals.addWidget(self._solde_lbl)
        totals.addStretch()

        layout = QVBoxLayout(self)
        layout.addLayout(top)
        layout.addLayout(totals)
        layout.addWidget(self._table)

        self._timer = QTimer(self)
        self._timer.timeout.connect(self.refresh)
        self._timer.start(60_000)
        self._load_caisses()
        self.refresh()

    def _load_caisses(self) -> None:
        try:
            rows = self._data.caisses()
        except Exception:  # noqa: BLE001
            return
        seen = {self._caisse.itemText(i) for i in range(self._caisse.count())}
        for r in rows:
            c = r.get("caisse")
            if c and c not in seen and c != "(globale)":
                self._caisse.addItem(c)
                seen.add(c)
        self._caisses_loaded = True

    def refresh(self) -> None:
        if not self._caisses_loaded:
            self._load_caisses()
        day = datetime.now().strftime("%Y-%m-%d")
        self._day_lbl.setText("<h3>%s</h3>" % day)
        caisse = self._caisse.currentText()
        caisse_arg = None if caisse == _GLOBALE else caisse
        try:
            rows = self._data.tresorerie_today(day, caisse_arg)
        except Exception:  # noqa: BLE001
            return

        # Cumuler par (magasin, mode) en séparant entrées / sorties.
        agg: dict = {}
        tot_e = tot_s = 0.0
        for r in rows:
            key = (r.get("store_id"), r.get("mode_paiement") or "?")
            a = agg.setdefault(key, {"entree": 0.0, "sortie": 0.0})
            val = float(r.get("total_encaisse") or 0)
            if r.get("sens") == "sortie":
                a["sortie"] += val; tot_s += val
            else:
                a["entree"] += val; tot_e += val

        self._entree_lbl.setText(fmt_da(tot_e))
        self._sortie_lbl.setText(fmt_da(tot_s))
        self._solde_lbl.setText(fmt_da(tot_e - tot_s))

        self._table.setRowCount(len(agg))
        for i, ((sid, mode), a) in enumerate(
                sorted(agg.items(), key=lambda x: (x[0][0] or 0, x[0][1]))):
            name = self._names.get(sid, "Magasin %s" % sid)
            self._table.setItem(i, 0, QTableWidgetItem(name))
            self._table.setItem(i, 1, QTableWidgetItem(mode))
            e = QTableWidgetItem(fmt_da(a["entree"], suffix="")); e.setForeground(Qt.green)
            e.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
            self._table.setItem(i, 2, e)
            s = QTableWidgetItem(fmt_da(a["sortie"], suffix=""))
            if a["sortie"]:
                s.setForeground(Qt.red)
            s.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
            self._table.setItem(i, 3, s)
