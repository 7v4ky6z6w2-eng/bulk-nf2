"""Page Synchronisation : journal des sessions + pending_ops en attente."""

from __future__ import annotations

import sqlite3

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QHBoxLayout, QLabel, QPushButton, QSplitter,
    QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)


class SyncStatusPage(QWidget):
    def __init__(self, db_con: sqlite3.Connection, store_names: dict, parent=None):
        super().__init__(parent)
        self._con = db_con
        self._names = store_names

        btn = QPushButton("Actualiser")
        btn.clicked.connect(self.refresh)

        # Sessions de sync
        self._sessions = QTableWidget(0, 7)
        self._sessions.setHorizontalHeaderLabels(
            ["#", "Magasin", "Démarré", "Terminé", "Lignes", "Statut", "Erreur"])
        self._sessions.setEditTriggers(QTableWidget.NoEditTriggers)

        # Ops en attente
        self._ops = QTableWidget(0, 5)
        self._ops.setHorizontalHeaderLabels(
            ["#", "Magasin", "Type", "Créé le", "Statut"])
        self._ops.setEditTriggers(QTableWidget.NoEditTriggers)

        splitter = QSplitter(Qt.Vertical)
        splitter.addWidget(self._make_section("Sessions", self._sessions))
        splitter.addWidget(self._make_section("Opérations en file", self._ops))

        top = QHBoxLayout()
        top.addWidget(QLabel("<h3>Synchronisation</h3>"))
        top.addStretch()
        top.addWidget(btn)

        layout = QVBoxLayout(self)
        layout.addLayout(top)
        layout.addWidget(splitter)

        self._timer = QTimer(self)
        self._timer.timeout.connect(self.refresh)
        self._timer.start(30_000)
        self.refresh()

    @staticmethod
    def _make_section(title: str, table: QTableWidget) -> QWidget:
        w = QWidget()
        v = QVBoxLayout(w)
        v.addWidget(QLabel("<b>%s</b>" % title))
        v.addWidget(table)
        return w

    def _status_item(self, status: str) -> QTableWidgetItem:
        item = QTableWidgetItem(status)
        colors = {"ok": "#28a745", "error": "#dc3545", "running": "#ffc107",
                  "applied": "#28a745", "failed": "#dc3545", "pending": "#6c757d"}
        item.setForeground(Qt.GlobalColor.white)
        return item

    def refresh(self) -> None:
        # Sessions
        logs = self._con.execute(
            "SELECT l.id, l.store_id, m.store_name, l.started, l.finished, "
            "       l.rows_pushed, l.status, l.error_msg "
            "FROM sync_log l LEFT JOIN store_meta m ON l.store_id=m.store_id "
            "ORDER BY l.id DESC LIMIT 50").fetchall()
        self._sessions.setRowCount(len(logs))
        for i, r in enumerate(logs):
            sid = r[1]
            name = self._names.get(sid, r[2] or ("M%d" % sid))
            self._sessions.setItem(i, 0, QTableWidgetItem(str(r[0])))
            self._sessions.setItem(i, 1, QTableWidgetItem(name))
            self._sessions.setItem(i, 2, QTableWidgetItem(str(r[3] or "")))
            self._sessions.setItem(i, 3, QTableWidgetItem(str(r[4] or "…")))
            n = QTableWidgetItem(str(r[5] or 0))
            n.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
            self._sessions.setItem(i, 4, n)
            self._sessions.setItem(i, 5, QTableWidgetItem(r[6] or ""))
            self._sessions.setItem(i, 6, QTableWidgetItem(r[7] or ""))

        # Pending ops
        ops = self._con.execute(
            "SELECT id, store_id, op_type, created_at, status FROM pending_ops "
            "ORDER BY id DESC LIMIT 50").fetchall()
        self._ops.setRowCount(len(ops))
        for i, r in enumerate(ops):
            sid = r[1]
            name = self._names.get(sid, "M%d" % sid)
            self._ops.setItem(i, 0, QTableWidgetItem(str(r[0])))
            self._ops.setItem(i, 1, QTableWidgetItem(name))
            self._ops.setItem(i, 2, QTableWidgetItem(r[2] or ""))
            self._ops.setItem(i, 3, QTableWidgetItem(str(r[3] or "")))
            self._ops.setItem(i, 4, QTableWidgetItem(r[4] or ""))
