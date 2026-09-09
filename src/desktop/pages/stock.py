"""Page Stock : quantité par PRODUIT et par MAGASIN, magasins côte à côte.

Une ligne par produit (regroupé via match_key, comme l'Éditeur de prix : même
code-barres ou lien manuel confirmé = même produit même sous des références
différentes selon le magasin), une colonne par magasin. Chaque colonne montre
le stock RÉEL de ce magasin (cumulé sur ses dépôts) — jamais un total combiné
entre magasins, pour pouvoir comparer les stocks au lieu de les mélanger.
"""

from __future__ import annotations

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QHBoxLayout, QLabel, QLineEdit, QPushButton,
    QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

from stores import StoreRegistry
from desktop.format import fmt_qty


class StockPage(QWidget):
    def __init__(self, registry: StoreRegistry, data, parent=None):
        super().__init__(parent)
        self._registry = registry
        self._data = data
        self._store_ids_ordered = [s.id for s in registry.stores]

        self._search = QLineEdit()
        self._search.setPlaceholderText("Rechercher par référence, désignation ou code-barres…")
        self._search.returnPressed.connect(self.refresh)
        btn = QPushButton("Actualiser")
        btn.clicked.connect(self.refresh)

        self._table = QTableWidget(0, 2 + len(self._store_ids_ordered))
        self._table.setHorizontalHeaderLabels(
            ["Référence", "Désignation"] + ["%s Qté" % s.name for s in registry.stores])
        self._table.setColumnWidth(1, 260)
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
            rows = self._data.stock_search(self._search.text().strip())
        except Exception:  # noqa: BLE001
            return

        # Regroupement par match_key, comme l'Éditeur de prix : le même
        # produit sous des références différentes selon le magasin devient
        # UNE ligne, avec une quantité PAR magasin (jamais sommée entre eux).
        by_key: dict[str, dict] = {}
        for r in rows:
            key = r.get("match_key") or r.get("ref_art")
            g = by_key.setdefault(key, {"desig": r.get("designation"), "refs": {}, "qty": {}})
            sid = r.get("store_id")
            g["refs"][sid] = r.get("ref_art")
            g["qty"][sid] = r.get("qte_stock")

        groups = list(by_key.values())
        store_ids = self._store_ids_ordered
        self._table.setSortingEnabled(False)
        self._table.setRowCount(len(groups))
        for i, g in enumerate(groups):
            refs = [g["refs"][sid] for sid in store_ids if sid in g["refs"]]
            uniq = sorted(set(refs))
            ref_txt = uniq[0] if len(uniq) == 1 else " / ".join(uniq)
            self._table.setItem(i, 0, QTableWidgetItem(ref_txt))
            self._table.setItem(i, 1, QTableWidgetItem(str(g["desig"] or "")))
            for col, sid in enumerate(store_ids, start=2):
                qty = g["qty"].get(sid)
                cell = QTableWidgetItem(fmt_qty(qty) if qty is not None else "—")
                cell.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
                if qty is not None and float(qty) <= 0:   # rupture ou anomalie : en rouge
                    cell.setForeground(Qt.red)
                self._table.setItem(i, col, cell)
        self._table.setSortingEnabled(True)
