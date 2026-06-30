"""Page Synchronisation : journal des sessions + file pending_ops (via le hub)."""

from __future__ import annotations

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QHBoxLayout, QLabel, QPushButton, QSplitter,
    QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)


class SyncStatusPage(QWidget):
    def __init__(self, data, store_names: dict, parent=None):
        super().__init__(parent)
        self._data = data
        self._names = store_names

        btn = QPushButton("Actualiser")
        btn.clicked.connect(self.refresh)

        self._sessions = QTableWidget(0, 7)
        self._sessions.setHorizontalHeaderLabels(
            ["#", "Magasin", "Démarré", "Terminé", "Lignes", "Statut", "Erreur"])
        self._sessions.setEditTriggers(QTableWidget.NoEditTriggers)

        self._ops = QTableWidget(0, 6)
        self._ops.setHorizontalHeaderLabels(
            ["#", "Magasin", "Type", "Créé le", "Statut", "Erreur"])
        self._ops.setEditTriggers(QTableWidget.NoEditTriggers)

        splitter = QSplitter(Qt.Vertical)
        splitter.addWidget(self._make_section("Sessions de synchronisation", self._sessions))
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

    def refresh(self) -> None:
        try:
            logs = self._data.sync_logs()
            ops = self._data.pending_ops_recent()
        except Exception:  # noqa: BLE001
            return

        self._sessions.setRowCount(len(logs))
        for i, r in enumerate(logs):
            sid = r.get("store_id")
            name = self._names.get(sid, r.get("store_name") or ("M%s" % sid))
            self._sessions.setItem(i, 0, QTableWidgetItem(str(r.get("id") or "")))
            self._sessions.setItem(i, 1, QTableWidgetItem(name))
            self._sessions.setItem(i, 2, QTableWidgetItem(str(r.get("started") or "")))
            self._sessions.setItem(i, 3, QTableWidgetItem(str(r.get("finished") or "…")))
            n = QTableWidgetItem(str(r.get("rows_pushed") or 0))
            n.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
            self._sessions.setItem(i, 4, n)
            self._sessions.setItem(i, 5, QTableWidgetItem(r.get("status") or ""))
            self._sessions.setItem(i, 6, QTableWidgetItem(r.get("error_msg") or ""))

        self._ops.setRowCount(len(ops))
        for i, r in enumerate(ops):
            sid = r.get("store_id")
            name = self._names.get(sid, "M%s" % sid)
            self._ops.setItem(i, 0, QTableWidgetItem(str(r.get("id") or "")))
            self._ops.setItem(i, 1, QTableWidgetItem(name))
            self._ops.setItem(i, 2, QTableWidgetItem(r.get("op_type") or ""))
            self._ops.setItem(i, 3, QTableWidgetItem(str(r.get("created_at") or "")))
            self._ops.setItem(i, 4, QTableWidgetItem(r.get("status") or ""))
            self._ops.setItem(i, 5, QTableWidgetItem(r.get("error_msg") or ""))
