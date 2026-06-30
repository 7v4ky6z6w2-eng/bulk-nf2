"""Fenêtre principale PrimeNF Hub — barre latérale + pages empilées.

L'application est un CLIENT du hub : aucune base locale. Toutes les données et
toutes les écritures passent par le hub via HTTP (objet HubData). Elle tourne
donc sur n'importe quel PC (y compris un PC personnel sans base ni Firebird) :
il suffit de stores.json et d'un accès réseau au magasin 1.
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QListWidget, QListWidgetItem, QHBoxLayout,
    QSplitter, QStackedWidget, QWidget,
)

from stores import StoreRegistry
from desktop.hub_data import HubData

from desktop.pages.overview import OverviewPage
from desktop.pages.stock import StockPage
from desktop.pages.ventes import VentesPage
from desktop.pages.tresorerie import TresoreriePage
from desktop.pages.sync_status import SyncStatusPage
from desktop.pages.bdr_page import BdrPage
from desktop.pages.prix_page import PrixPage


# (label, classe, besoin_du_registre)
_PAGES = [
    ("Vue d'ensemble", OverviewPage, False),
    ("Stock", StockPage, False),
    ("Ventes", VentesPage, False),
    ("Trésorerie", TresoreriePage, False),
    ("Import BDR", BdrPage, True),
    ("Éditeur de prix", PrixPage, True),
    ("Synchronisation", SyncStatusPage, False),
]


class MainWindow(QWidget):
    def __init__(self, registry: StoreRegistry, data: HubData):
        super().__init__()
        self.setWindowTitle("PrimeNF Hub")
        self.resize(1100, 700)

        self._data = data
        names = registry.names()

        self._nav = QListWidget()
        self._nav.setFixedWidth(160)
        self._nav.setStyleSheet(
            "QListWidget { background:#1a1d20; border:none; }"
            "QListWidget::item { color:#adb5bd; padding:8px 12px; border-radius:6px; }"
            "QListWidget::item:selected { color:white; background:#0d6efd44; }"
            "QListWidget::item:hover { background:#ffffff11; }")

        self._stack = QStackedWidget()

        for label, PageClass, needs_registry in _PAGES:
            item = QListWidgetItem(label)
            self._nav.addItem(item)
            if needs_registry:
                page = PageClass(registry, data)
            else:
                page = PageClass(data, names)
            self._stack.addWidget(page)

        self._nav.currentRowChanged.connect(self._stack.setCurrentIndex)
        self._nav.setCurrentRow(0)

        splitter = QSplitter(Qt.Horizontal)
        splitter.addWidget(self._nav)
        splitter.addWidget(self._stack)
        splitter.setStretchFactor(1, 1)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(splitter)
