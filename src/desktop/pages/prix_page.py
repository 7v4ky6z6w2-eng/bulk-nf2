"""Page Éditeur de prix : recherche (via hub) + soumission au hub (1 ou 3 magasins).

Aucun accès Firebird ici : la recherche d'articles et l'application des prix
passent par le hub. Pour chaque magasin sélectionné, le hub applique tout de
suite (en ligne) ou met en file (hors ligne). TTC = HT (pas de TVA).
"""

from __future__ import annotations

from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtWidgets import (
    QCheckBox, QDoubleSpinBox, QFormLayout, QGroupBox,
    QHBoxLayout, QLabel, QLineEdit, QMessageBox,
    QPushButton, QTableWidget, QTableWidgetItem, QTextEdit,
    QVBoxLayout, QWidget,
)

from stores import StoreRegistry


class _PriceThread(QThread):
    progress = Signal(int, str, str)   # store_id, status, message
    finished_all = Signal()

    def __init__(self, data, store_ids: list, changes: list):
        super().__init__()
        self._data = data
        self._store_ids = store_ids
        self._changes = changes

    def run(self) -> None:
        for sid in self._store_ids:
            try:
                res = self._data.submit_op(sid, "price_update", {"changes": self._changes})
                status = res.get("status", "error")
                if status == "applied":
                    msg = "Magasin %d : appliqué (%s)." % (sid, res.get("count", "?"))
                elif status == "queued":
                    msg = "Magasin %d : hors ligne, mis en file (op #%s)." % (
                        sid, res.get("op_id", "?"))
                else:
                    msg = "Magasin %d : %s" % (sid, res.get("error", "erreur"))
                self.progress.emit(sid, status, msg)
            except Exception as exc:  # noqa: BLE001
                self.progress.emit(sid, "error", "Magasin %d : hub injoignable (%s)" % (sid, exc))
        self.finished_all.emit()


class PrixPage(QWidget):
    def __init__(self, registry: StoreRegistry, data, parent=None):
        super().__init__(parent)
        self._registry = registry
        self._data = data
        self._thread: _PriceThread | None = None

        # ── Recherche article ──
        self._search = QLineEdit()
        self._search.setPlaceholderText("Référence ou désignation…")
        self._search.returnPressed.connect(self._do_search)
        search_btn = QPushButton("Chercher")
        search_btn.clicked.connect(self._do_search)

        # Colonnes de prix generees dynamiquement (une par magasin connu de
        # stores.json) : pas de limite fixe, un magasin ajoute plus tard
        # apparait sans recompiler l'application.
        self._store_ids_ordered = [s.id for s in registry.stores]
        self._results = QTableWidget(0, 2 + len(self._store_ids_ordered))
        self._results.setHorizontalHeaderLabels(
            ["Référence", "Désignation"] + ["%s Prix" % s.name for s in registry.stores])
        self._results.setEditTriggers(QTableWidget.NoEditTriggers)
        self._results.setSelectionBehavior(QTableWidget.SelectRows)
        self._results.setMaximumHeight(220)

        # ── Nouveau prix ──
        self._new_price = QDoubleSpinBox()
        self._new_price.setRange(0, 9_999_999)
        self._new_price.setDecimals(2)
        self._new_price.setSuffix(" DA")

        # ── Scope magasins ──
        self._store_checks: dict[int, QCheckBox] = {}
        scope_layout = QHBoxLayout()
        for s in registry.stores:
            cb = QCheckBox(s.name)
            cb.setChecked(True)
            self._store_checks[s.id] = cb
            scope_layout.addWidget(cb)

        self._apply_btn = QPushButton("Appliquer le prix")
        self._apply_btn.clicked.connect(self._do_apply)

        self._log = QTextEdit()
        self._log.setReadOnly(True)
        self._log.setMaximumHeight(160)

        # ── Layout ──
        search_row = QHBoxLayout()
        search_row.addWidget(self._search, 1)
        search_row.addWidget(search_btn)

        form = QFormLayout()
        form.addRow("Nouveau prix (HT = TTC) :", self._new_price)

        scope_box = QGroupBox("Appliquer sur :")
        scope_box.setLayout(scope_layout)

        action_row = QHBoxLayout()
        action_row.addStretch()
        action_row.addWidget(self._apply_btn)

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("<h3>Éditeur de prix</h3>"))
        layout.addLayout(search_row)
        layout.addWidget(self._results)
        layout.addLayout(form)
        layout.addWidget(scope_box)
        layout.addLayout(action_row)
        layout.addWidget(QLabel("Journal :"))
        layout.addWidget(self._log)

    def _log_msg(self, msg: str) -> None:
        self._log.append(msg)

    def _do_search(self) -> None:
        try:
            rows = self._data.article_search(self._search.text().strip())
        except Exception as exc:  # noqa: BLE001
            self._log_msg("Hub injoignable : %s" % exc)
            return
        store_ids = self._store_ids_ordered

        by_ref: dict[str, dict] = {}
        for r in rows:
            ref = r.get("ref_art")
            if ref not in by_ref:
                by_ref[ref] = {"ref": ref, "desig": r.get("designation"), "prices": {}}
            by_ref[ref]["prices"][r.get("store_id")] = r.get("prixventeht")

        items = list(by_ref.values())
        self._results.setRowCount(len(items))
        for i, item in enumerate(items):
            self._results.setItem(i, 0, QTableWidgetItem(str(item["ref"])))
            self._results.setItem(i, 1, QTableWidgetItem(str(item["desig"] or "")))
            for col, sid in enumerate(store_ids, start=2):
                price = item["prices"].get(sid)
                cell = QTableWidgetItem("%.2f" % float(price) if price is not None else "—")
                cell.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
                self._results.setItem(i, col, cell)

    def _do_apply(self) -> None:
        # Un clic pendant qu'un envoi precedent tourne encore remplacerait
        # self._thread par un nouveau QThread pendant que l'ancien tourne
        # toujours -> PySide detruit l'ancien objet Python alors que le
        # thread C++ sous-jacent est actif -> crash. Le bouton desactive
        # tant que _on_all_done n'a pas confirme la fin evite ce cas,
        # meme en cliquant tres vite plusieurs fois de suite.
        if self._thread is not None and self._thread.isRunning():
            return
        if not self._results.selectedItems():
            QMessageBox.warning(self, "Aucune sélection", "Sélectionnez un article dans la liste.")
            return
        row = self._results.currentRow()
        ref_art = self._results.item(row, 0).text()
        new_ht = self._new_price.value()
        if new_ht <= 0:
            QMessageBox.warning(self, "Prix invalide", "Le prix doit être > 0.")
            return

        selected = [sid for sid, cb in self._store_checks.items() if cb.isChecked()]
        if not selected:
            QMessageBox.warning(self, "Scope vide", "Sélectionnez au moins un magasin.")
            return

        store_names = ", ".join(self._registry.get(sid).name for sid in selected)
        confirm = QMessageBox.question(
            self, "Confirmer le changement de prix",
            "Appliquer le prix %.2f DA pour l'article %s sur : %s ?\n\n"
            "Si un magasin est en ligne, ce changement sera écrit immédiatement." % (
                new_ht, ref_art, store_names),
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        if confirm != QMessageBox.Yes:
            return

        self._log_msg("Prix %.2f DA pour %s → %d magasin(s)…" % (new_ht, ref_art, len(selected)))
        changes = [{"ref0": ref_art, "values": {"PRIXVENTEHT": new_ht, "PRIXVENTETTC": new_ht}}]

        self._apply_btn.setEnabled(False)
        self._thread = _PriceThread(self._data, selected, changes)
        self._thread.progress.connect(self._on_progress)
        self._thread.finished_all.connect(self._on_all_done)
        self._thread.start()

    def _on_progress(self, store_id: int, status: str, msg: str) -> None:
        tag = {"applied": "[OK] ", "queued": "[FILE] "}.get(status, "[ERREUR] ")
        self._log_msg(tag + msg)

    def _on_all_done(self) -> None:
        self._log_msg("Terminé.")
        self._apply_btn.setEnabled(True)
