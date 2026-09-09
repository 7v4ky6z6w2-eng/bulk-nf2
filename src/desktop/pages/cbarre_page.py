"""Page Codes-barres : gérer les codes-barres équivalents (EQUIV_CBARRES).

Netfact2 affiche/scanne les codes de la table EQUIV_CBARRES (pas ARTICLE.
CODE_BARRE(35)/CODE_BARRES(60), invisibles). Cette page permet d'ajouter
plusieurs codes-barres à un article et d'en retirer, sur 1 ou plusieurs
magasins. En ligne → appliqué tout de suite ; hors ligne → mis en file.
"""

from __future__ import annotations

from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtWidgets import (
    QCheckBox, QGroupBox, QHBoxLayout, QLabel, QLineEdit, QListWidget,
    QListWidgetItem, QMessageBox, QPushButton, QTableWidget, QTableWidgetItem,
    QVBoxLayout, QWidget,
)

from stores import StoreRegistry


class _OpThread(QThread):
    progress = Signal(int, str, str)   # store_id, status, message
    finished_all = Signal()

    def __init__(self, data, store_ids: list, ops: list):
        super().__init__()
        self._data = data
        self._store_ids = store_ids
        self._ops = ops

    def run(self) -> None:
        for sid in self._store_ids:
            try:
                res = self._data.submit_op(sid, "barcode_ops", {"ops": self._ops})
                status = res.get("status", "error")
                if status == "applied":
                    msg = "Magasin %d : appliqué (%s opération(s))." % (sid, res.get("count", "?"))
                elif status == "queued":
                    msg = "Magasin %d : hors ligne, mis en file (op #%s)." % (sid, res.get("op_id", "?"))
                else:
                    msg = "Magasin %d : %s" % (sid, res.get("error", "erreur"))
                self.progress.emit(sid, status, msg)
            except Exception as exc:  # noqa: BLE001
                self.progress.emit(sid, "error", "Magasin %d : hub injoignable (%s)" % (sid, exc))
        self.finished_all.emit()


class CbarrePage(QWidget):
    def __init__(self, registry: StoreRegistry, data, parent=None):
        super().__init__(parent)
        self._registry = registry
        self._data = data
        self._ref = None
        self._original: set = set()   # codes-barres connus (pour calculer le diff)
        self._thread: _OpThread | None = None

        # Recherche article
        self._search = QLineEdit()
        self._search.setPlaceholderText("Référence ou désignation…")
        self._search.returnPressed.connect(self._do_search)
        search_btn = QPushButton("Chercher")
        search_btn.clicked.connect(self._do_search)

        self._results = QTableWidget(0, 2)
        self._results.setHorizontalHeaderLabels(["Référence", "Désignation"])
        self._results.setEditTriggers(QTableWidget.NoEditTriggers)
        self._results.setSelectionBehavior(QTableWidget.SelectRows)
        self._results.setMaximumHeight(150)
        self._results.itemSelectionChanged.connect(self._on_pick_article)

        # Liste des codes-barres de l'article
        self._selected_lbl = QLabel("Aucun article sélectionné")
        self._barcodes = QListWidget()
        self._new_bc = QLineEdit()
        self._new_bc.setPlaceholderText("Nouveau code-barres…")
        self._new_bc.returnPressed.connect(self._add_barcode)
        add_bc_btn = QPushButton("Ajouter")
        add_bc_btn.clicked.connect(self._add_barcode)
        del_bc_btn = QPushButton("Retirer le sélectionné")
        del_bc_btn.clicked.connect(self._remove_selected)

        # Scope magasins
        self._store_checks: dict[int, QCheckBox] = {}
        scope_layout = QHBoxLayout()
        for s in registry.stores:
            cb = QCheckBox(s.name); cb.setChecked(True)
            self._store_checks[s.id] = cb
            scope_layout.addWidget(cb)
        scope_box = QGroupBox("Appliquer sur :"); scope_box.setLayout(scope_layout)

        self._apply_btn = QPushButton("Enregistrer les changements")
        self._apply_btn.clicked.connect(self._do_apply)

        self._log = QLabel(""); self._log.setWordWrap(True)

        # Layout
        search_row = QHBoxLayout()
        search_row.addWidget(self._search, 1); search_row.addWidget(search_btn)

        bc_row = QHBoxLayout()
        bc_row.addWidget(self._new_bc, 1); bc_row.addWidget(add_bc_btn); bc_row.addWidget(del_bc_btn)

        action_row = QHBoxLayout()
        action_row.addStretch(); action_row.addWidget(self._apply_btn)

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("<h3>Codes-barres (équivalents)</h3>"))
        layout.addLayout(search_row)
        layout.addWidget(self._results)
        layout.addWidget(self._selected_lbl)
        layout.addWidget(self._barcodes)
        layout.addLayout(bc_row)
        layout.addWidget(scope_box)
        layout.addLayout(action_row)
        layout.addWidget(self._log)

    # -- recherche ---------------------------------------------------------
    def _do_search(self) -> None:
        try:
            rows = self._data.article_search(self._search.text().strip())
        except Exception as exc:  # noqa: BLE001
            self._log.setText("Hub injoignable : %s" % exc)
            return
        seen, items = set(), []
        for r in rows:
            ref = r.get("ref_art")
            if ref not in seen:
                seen.add(ref); items.append((ref, r.get("designation") or ""))
        self._results.setRowCount(len(items))
        for i, (ref, des) in enumerate(items):
            self._results.setItem(i, 0, QTableWidgetItem(str(ref)))
            self._results.setItem(i, 1, QTableWidgetItem(str(des)))

    def _on_pick_article(self) -> None:
        row = self._results.currentRow()
        if row < 0:
            return
        self._ref = self._results.item(row, 0).text()
        self._selected_lbl.setText("Article : <b>%s</b>" % self._ref)
        self._barcodes.clear()
        self._original = set()
        try:
            rows = self._data.article_barcodes(self._ref)
        except Exception:  # noqa: BLE001
            rows = []
        # Union des codes-barres tous magasins (les produits sont identiques).
        for r in rows:
            bc = str(r.get("code_barres") or "").strip()
            if bc and bc not in self._original:
                self._original.add(bc)
                self._barcodes.addItem(QListWidgetItem(bc))

    # -- édition de la liste ------------------------------------------------
    def _add_barcode(self) -> None:
        bc = self._new_bc.text().strip()
        if not bc:
            return
        existing = {self._barcodes.item(i).text() for i in range(self._barcodes.count())}
        if bc not in existing:
            self._barcodes.addItem(QListWidgetItem(bc))
        self._new_bc.clear()

    def _remove_selected(self) -> None:
        for item in self._barcodes.selectedItems():
            self._barcodes.takeItem(self._barcodes.row(item))

    # -- application --------------------------------------------------------
    def _do_apply(self) -> None:
        # Cf. prix_page.py : un clic pendant qu'un envoi precedent tourne
        # encore remplacerait self._thread par un nouveau QThread pendant que
        # l'ancien tourne toujours -> crash PySide (thread C++ actif detruit).
        # Le bouton desactive tant que le thread n'a pas fini l'empeche.
        if self._thread is not None and self._thread.isRunning():
            return
        if not self._ref:
            QMessageBox.warning(self, "Aucun article", "Sélectionnez d'abord un article.")
            return
        current = {self._barcodes.item(i).text() for i in range(self._barcodes.count())}
        to_add = current - self._original
        to_remove = self._original - current
        if not to_add and not to_remove:
            QMessageBox.information(self, "Rien à faire", "Aucun changement de code-barres.")
            return
        ops = ([{"action": "add", "ref_art": self._ref, "barcode": b} for b in to_add]
               + [{"action": "remove", "ref_art": self._ref, "barcode": b} for b in to_remove])

        selected = [sid for sid, cb in self._store_checks.items() if cb.isChecked()]
        if not selected:
            QMessageBox.warning(self, "Scope vide", "Sélectionnez au moins un magasin.")
            return

        store_names = ", ".join(self._registry.get(sid).name for sid in selected)
        detail_lines = []
        if to_add:
            detail_lines.append("Ajouter : " + ", ".join(sorted(to_add)))
        if to_remove:
            detail_lines.append("Retirer : " + ", ".join(sorted(to_remove)))
        confirm = QMessageBox.question(
            self, "Confirmer les changements de codes-barres",
            "Article %s — %s\nMagasin(s) : %s\n\n"
            "Si un magasin est en ligne, ce changement sera écrit immédiatement." % (
                self._ref, "\n".join(detail_lines), store_names),
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        if confirm != QMessageBox.Yes:
            return

        self._log.setText("Envoi : +%d / -%d code(s) sur %d magasin(s)…" % (
            len(to_add), len(to_remove), len(selected)))
        self._apply_btn.setEnabled(False)
        self._thread = _OpThread(self._data, selected, ops)
        self._thread.progress.connect(self._on_progress)
        self._thread.finished_all.connect(self._on_apply_done)
        self._thread.start()

    def _on_apply_done(self) -> None:
        self._apply_btn.setEnabled(True)
        self._on_pick_article()

    def _on_progress(self, store_id: int, status: str, msg: str) -> None:
        prev = self._log.text()
        tag = {"applied": "[OK] ", "queued": "[FILE] "}.get(status, "[ERREUR] ")
        self._log.setText((prev + "\n" if prev else "") + tag + msg)
