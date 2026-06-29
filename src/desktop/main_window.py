"""Fenêtre principale PrimeNF Hub — barre latérale + pages empilées."""

from __future__ import annotations

import sqlite3

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QListWidget, QListWidgetItem, QHBoxLayout,
    QSplitter, QStackedWidget, QWidget,
)

from stores import StoreRegistry
from hub.central_db import connect, init_db

from desktop.pages.overview import OverviewPage
from desktop.pages.stock import StockPage
from desktop.pages.ventes import VentesPage
from desktop.pages.tresorerie import TresoreriePage
from desktop.pages.sync_status import SyncStatusPage
from desktop.pages.bdr_page import BdrPage
from desktop.pages.prix_page import PrixPage


_PAGES = [
    ("Vue d'ensemble", OverviewPage),
    ("Stock", StockPage),
    ("Ventes", VentesPage),
    ("Trésorerie", TresoreriePage),
    ("Import BDR", BdrPage),
    ("Éditeur de prix", PrixPage),
    ("Synchronisation", SyncStatusPage),
]


class MainWindow(QWidget):
    def __init__(self, registry: StoreRegistry, db_path: str):
        super().__init__()
        self.setWindowTitle("PrimeNF Hub")
        self.resize(1100, 700)

        init_db(db_path)
        self._con: sqlite3.Connection = connect(db_path)
        names = registry.names()

        # Sidebar
        self._nav = QListWidget()
        self._nav.setFixedWidth(160)
        self._nav.setStyleSheet(
            "QListWidget { background:#1a1d20; border:none; }"
            "QListWidget::item { color:#adb5bd; padding:8px 12px; border-radius:6px; }"
            "QListWidget::item:selected { color:white; background:#0d6efd44; }"
            "QListWidget::item:hover { background:#ffffff11; }")

        self._stack = QStackedWidget()

        for label, PageClass in _PAGES:
            item = QListWidgetItem(label)
            self._nav.addItem(item)
            if PageClass in (OverviewPage, StockPage, VentesPage,
                             TresoreriePage, SyncStatusPage):
                page = PageClass(self._con, names)
            else:
                page = PageClass(registry, self._con)
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

    def closeEvent(self, event):
        try:
            self._con.close()
        except Exception:
            pass
        super().closeEvent(event)
