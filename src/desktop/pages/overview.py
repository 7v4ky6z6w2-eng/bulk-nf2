"""Page Vue d'ensemble : état des magasins (données via le hub)."""

from __future__ import annotations

from datetime import datetime

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QGridLayout, QGroupBox, QLabel, QScrollArea, QVBoxLayout, QWidget,
)


def _ago(ts: str | None) -> str:
    if not ts:
        return "jamais"
    try:
        dt = datetime.fromisoformat(ts)
        delta = datetime.now(dt.tzinfo) - dt
        m = int(delta.total_seconds() // 60)
        if m < 2:
            return "à l'instant"
        if m < 60:
            return "il y a %dm" % m
        h = m // 60
        if h < 24:
            return "il y a %dh" % h
        return "il y a %dj" % (h // 24)
    except Exception:
        return ts or "?"


class StoreCard(QGroupBox):
    def __init__(self, name: str, parent=None):
        super().__init__(name, parent)
        self._lbl_sync = QLabel("—")
        self._lbl_seen = QLabel("—")
        self._lbl_badge = QLabel()

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("<b>Dernière synchro :</b>"))
        layout.addWidget(self._lbl_sync)
        layout.addWidget(QLabel("<b>Dernier contact :</b>"))
        layout.addWidget(self._lbl_seen)
        layout.addWidget(self._lbl_badge)

        for lbl in (self._lbl_sync, self._lbl_seen, self._lbl_badge):
            lbl.setTextFormat(Qt.RichText)

    def update_data(self, s: dict) -> None:
        self._lbl_sync.setText(_ago(s.get("last_ok")))
        self._lbl_seen.setText(_ago(s.get("last_seen")))
        ok = bool(s.get("last_ok"))
        color, txt = ("#28a745", "● Actif") if ok else ("#6c757d", "● Jamais synchro")
        self._lbl_badge.setText("<span style='color:%s'>%s</span>" % (color, txt))


class OverviewPage(QWidget):
    def __init__(self, data, store_names: dict, parent=None):
        super().__init__(parent)
        self._data = data
        self._names = store_names
        self._cards: dict[int, StoreCard] = {}

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        inner = QWidget()
        self._grid = QGridLayout(inner)
        self._grid.setAlignment(Qt.AlignTop)
        scroll.setWidget(inner)

        self._status = QLabel()
        self._status.setStyleSheet("color:#dc3545;")

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("<h3>Vue d'ensemble</h3>"))
        layout.addWidget(self._status)
        layout.addWidget(scroll)

        self._timer = QTimer(self)
        self._timer.timeout.connect(self.refresh)
        self._timer.start(30_000)
        self.refresh()

    def refresh(self) -> None:
        try:
            statuses = self._data.store_status()
            self._status.setText("")
        except Exception as exc:  # noqa: BLE001
            self._status.setText("Hub injoignable : %s" % exc)
            return
        for s in statuses:
            sid = s["store_id"]
            name = self._names.get(sid, s.get("store_name") or "Magasin %d" % sid)
            if sid not in self._cards:
                card = StoreCard(name)
                self._cards[sid] = card
                row, col = divmod(len(self._cards) - 1, 3)
                self._grid.addWidget(card, row, col)
            self._cards[sid].update_data(s)
