"""Widget réutilisable : sélecteur de magasin avec badge en ligne / hors ligne."""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QComboBox, QHBoxLayout, QLabel, QWidget

from stores import StoreRegistry
from hub.write_back import is_reachable


class StorePicker(QWidget):
    store_changed = Signal(int)  # store_id sélectionné

    def __init__(self, registry: StoreRegistry, parent=None):
        super().__init__(parent)
        self._registry = registry
        self._combo = QComboBox()
        self._badge = QLabel()

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(QLabel("Magasin :"))
        layout.addWidget(self._combo)
        layout.addWidget(self._badge)
        layout.addStretch()

        for s in registry.stores:
            self._combo.addItem(s.name, s.id)

        self._combo.currentIndexChanged.connect(self._on_change)
        self._on_change(0)

    def _on_change(self, _idx: int) -> None:
        sid = self.current_id()
        if sid is None:
            return
        try:
            store = self._registry.get(sid)
            online = is_reachable(store.host, store.port)
        except Exception:  # noqa: BLE001
            online = False
        if online:
            self._badge.setText("<span style='color:#28a745'>● En ligne</span>")
        else:
            self._badge.setText("<span style='color:#dc3545'>● Hors ligne</span>")
        self._badge.setTextFormat(Qt.RichText)
        self.store_changed.emit(sid)

    def current_id(self) -> int | None:
        return self._combo.currentData()

    def refresh(self) -> None:
        self._on_change(self._combo.currentIndex())
