"""Fenêtre principale PrimeNF Hub — barre latérale + pages empilées.

L'application est un CLIENT du hub : aucune base locale. Toutes les données et
toutes les écritures passent par le hub via HTTP (objet HubData). Elle tourne
donc sur n'importe quel PC (y compris un PC personnel sans base ni Firebird) :
il suffit de stores.json et d'un accès réseau au magasin 1.
"""

from __future__ import annotations

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QLabel, QListWidget, QListWidgetItem, QHBoxLayout,
    QSplitter, QStackedWidget, QVBoxLayout, QWidget,
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
from desktop.pages.cbarre_page import CbarrePage


# (label, classe, besoin_du_registre)
_PAGES = [
    ("Vue d'ensemble", OverviewPage, False),
    ("Stock", StockPage, False),
    ("Ventes", VentesPage, False),
    ("Trésorerie", TresoreriePage, False),
    ("Import BDR", BdrPage, True),
    ("Éditeur de prix", PrixPage, True),
    ("Codes-barres", CbarrePage, True),
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

        # Indicateur d'état du hub, toujours visible en bas de la barre latérale.
        self._hub_status = QLabel()
        self._hub_status.setTextFormat(Qt.RichText)
        self._hub_status.setStyleSheet("padding:8px 12px; background:#1a1d20;")

        side = QWidget()
        side_layout = QVBoxLayout(side)
        side_layout.setContentsMargins(0, 0, 0, 0)
        side_layout.setSpacing(0)
        side_layout.addWidget(self._nav, 1)
        side_layout.addWidget(self._hub_status)
        side.setFixedWidth(160)

        splitter = QSplitter(Qt.Horizontal)
        splitter.addWidget(side)
        splitter.addWidget(self._stack)
        splitter.setStretchFactor(1, 1)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(splitter)

        self._ping_timer = QTimer(self)
        self._ping_timer.timeout.connect(self._refresh_hub_status)
        self._ping_timer.start(30_000)
        self._refresh_hub_status()

    def _refresh_hub_status(self) -> None:
        from datetime import datetime
        ok = False
        try:
            ok = self._data.ping()
        except Exception:  # noqa: BLE001
            ok = False
        ts = datetime.now().strftime("%H:%M:%S")
        if ok:
            self._hub_status.setText(
                "<span style='color:#28a745'>●</span> "
                "<span style='color:#adb5bd'>Hub connecté<br>"
                "<small>actualisé %s</small></span>" % ts)
        else:
            self._hub_status.setText(
                "<span style='color:#dc3545'>●</span> "
                "<span style='color:#adb5bd'>Hub injoignable<br>"
                "<small>essai %s</small></span>" % ts)
