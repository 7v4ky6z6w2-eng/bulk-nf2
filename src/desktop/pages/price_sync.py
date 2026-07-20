"""Dialogues auxiliaires de l'Éditeur de prix : correspondances par nom et
synchronisation groupée des prix depuis un magasin source.

Ces deux outils s'appuient sur `match_key` (cf. central_db.py côté hub) :
- MatchSuggestionsDialog trouve des articles de MÊME désignation dans
  plusieurs magasins qui n'ont PAS encore de match_key commun (ni référence,
  ni code-barres partagé) et laisse l'utilisateur confirmer manuellement
  qu'il s'agit du même produit (article_link) — jamais automatique, pour
  éviter de fusionner deux produits différents qui portent le même nom.
- PriceSyncDialog liste, pour un magasin source choisi, tous les écarts de
  prix de vente avec les autres magasins sur les groupes déjà unifiés, et
  permet de les appliquer en masse (avec sélection ligne par ligne).
"""

from __future__ import annotations

from PySide6.QtCore import QThread, Signal
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QHBoxLayout, QLabel, QListWidget,
    QMessageBox, QPushButton, QTableWidget, QTableWidgetItem, QVBoxLayout,
)


class MatchSuggestionsDialog(QDialog):
    def __init__(self, registry, data, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Suggestions de correspondance par nom")
        self._registry = registry
        self._data = data
        self._groups: list = []

        self._list = QListWidget()
        self._list.itemSelectionChanged.connect(self._on_select)

        self._detail = QTableWidget(0, 3)
        self._detail.setHorizontalHeaderLabels(["Magasin", "Référence", "Prix"])
        self._detail.setEditTriggers(QTableWidget.NoEditTriggers)

        refresh_btn = QPushButton("Rafraîchir")
        refresh_btn.clicked.connect(self._load)
        self._link_btn = QPushButton("Confirmer : c'est le même article")
        self._link_btn.clicked.connect(self._confirm)

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel(
            "Articles portant le même nom dans plusieurs magasins, pas encore\n"
            "reliés (référence et code-barres différents). Rien n'est fusionné\n"
            "automatiquement : vérifiez puis confirmez chaque groupe."))
        layout.addWidget(refresh_btn)
        layout.addWidget(self._list, 1)
        layout.addWidget(self._detail)
        layout.addWidget(self._link_btn)
        self.resize(640, 520)
        self._load()

    def _load(self) -> None:
        try:
            self._groups = self._data.name_match_suggestions()
        except Exception as exc:  # noqa: BLE001
            QMessageBox.warning(self, "Hub injoignable", str(exc))
            return
        self._list.clear()
        for g in self._groups:
            self._list.addItem("%s  (%d magasins)" % (g["designation"], len(g["items"])))
        self._detail.setRowCount(0)

    def _on_select(self) -> None:
        row = self._list.currentRow()
        if row < 0 or row >= len(self._groups):
            self._detail.setRowCount(0)
            return
        items = self._groups[row]["items"]
        names = self._registry.names()
        self._detail.setRowCount(len(items))
        for i, it in enumerate(items):
            self._detail.setItem(i, 0, QTableWidgetItem(names.get(it["store_id"], str(it["store_id"]))))
            self._detail.setItem(i, 1, QTableWidgetItem(str(it["ref_art"])))
            price = it.get("prixventeht")
            self._detail.setItem(i, 2, QTableWidgetItem("%.2f" % price if price is not None else "—"))

    def _confirm(self) -> None:
        row = self._list.currentRow()
        if row < 0 or row >= len(self._groups):
            QMessageBox.warning(self, "Aucune sélection", "Sélectionnez une suggestion dans la liste.")
            return
        group = self._groups[row]
        members = [{"store_id": it["store_id"], "ref_art": it["ref_art"]} for it in group["items"]]
        confirm = QMessageBox.question(
            self, "Confirmer la correspondance",
            "Confirmer que ces %d articles sont le MÊME produit ?\n\n« %s »\n\n"
            "Ils apparaîtront alors comme UNE seule ligne dans l'Éditeur de prix."
            % (len(members), group["designation"]),
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        if confirm != QMessageBox.Yes:
            return
        try:
            self._data.confirm_link(members)
        except Exception as exc:  # noqa: BLE001
            QMessageBox.warning(self, "Hub injoignable", str(exc))
            return
        self._groups.pop(row)
        self._list.takeItem(row)
        self._detail.setRowCount(0)


class _SyncThread(QThread):
    progress = Signal(str)
    finished_all = Signal()

    def __init__(self, data, rows: list):
        super().__init__()
        self._data = data
        self._rows = rows

    def run(self) -> None:
        ok, fail = 0, 0
        for r in self._rows:
            try:
                changes = [{"ref0": r["ref_art"],
                           "values": {"PRIXVENTEHT": r["source_price"],
                                      "PRIXVENTETTC": r["source_price"]}}]
                res = self._data.submit_op(r["store_id"], "price_update", {"changes": changes})
                if res.get("status") in ("applied", "queued"):
                    ok += 1
                else:
                    fail += 1
            except Exception:  # noqa: BLE001
                fail += 1
            self.progress.emit("%d/%d traité(s) — %d ok, %d échec(s)"
                               % (ok + fail, len(self._rows), ok, fail))
        self.finished_all.emit()


class PriceSyncDialog(QDialog):
    def __init__(self, registry, data, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Synchroniser les prix depuis un magasin")
        self._registry = registry
        self._data = data
        self._thread: _SyncThread | None = None
        self._rows: list = []

        self._source = QComboBox()
        for s in registry.stores:
            self._source.addItem(s.name, s.id)
        load_btn = QPushButton("Voir les écarts")
        load_btn.clicked.connect(self._load)

        self._table = QTableWidget(0, 5)
        self._table.setHorizontalHeaderLabels(
            ["", "Désignation", "Magasin cible", "Prix actuel", "Nouveau prix"])
        self._table.setEditTriggers(QTableWidget.NoEditTriggers)

        self._apply_btn = QPushButton("Appliquer les changements cochés")
        self._apply_btn.clicked.connect(self._apply)

        self._status = QLabel("")

        top = QHBoxLayout()
        top.addWidget(QLabel("Magasin source :"))
        top.addWidget(self._source)
        top.addWidget(load_btn)
        top.addStretch()

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel(
            "Les prix du magasin source seront copiés sur chaque magasin cible\n"
            "coché ci-dessous, pour les articles déjà reconnus comme le même\n"
            "produit (référence commune, code-barres partagé, ou correspondance\n"
            "confirmée dans « Suggestions de correspondance »)."))
        layout.addLayout(top)
        layout.addWidget(self._table, 1)
        layout.addWidget(self._apply_btn)
        layout.addWidget(self._status)
        self.resize(760, 520)

    def _load(self) -> None:
        source_id = self._source.currentData()
        try:
            groups = self._data.price_sync_candidates(source_id)
        except Exception as exc:  # noqa: BLE001
            self._status.setText("Hub injoignable : %s" % exc)
            return
        self._rows = []
        for g in groups:
            for t in g["targets"]:
                self._rows.append({
                    "designation": g["designation"], "source_price": g["source_price"],
                    "store_id": t["store_id"], "ref_art": t["ref_art"],
                    "current_price": t["current_price"],
                })
        names = self._registry.names()
        self._table.setRowCount(len(self._rows))
        for i, r in enumerate(self._rows):
            cb = QCheckBox()
            cb.setChecked(True)
            self._table.setCellWidget(i, 0, cb)
            self._table.setItem(i, 1, QTableWidgetItem(str(r["designation"] or "")))
            self._table.setItem(i, 2, QTableWidgetItem(names.get(r["store_id"], str(r["store_id"]))))
            cur = r["current_price"]
            self._table.setItem(i, 3, QTableWidgetItem("%.2f" % cur if cur is not None else "—"))
            self._table.setItem(i, 4, QTableWidgetItem("%.2f" % r["source_price"]))
        self._status.setText("%d écart(s) trouvé(s)." % len(self._rows)
                             if self._rows else "Aucun écart : les prix sont déjà alignés.")

    def _apply(self) -> None:
        # Même précaution que prix_page.py : ne jamais remplacer un QThread
        # encore actif (crash PySide), le bouton reste désactivé pendant l'envoi.
        if self._thread is not None and self._thread.isRunning():
            return
        selected = [r for i, r in enumerate(self._rows)
                   if self._table.cellWidget(i, 0) and self._table.cellWidget(i, 0).isChecked()]
        if not selected:
            QMessageBox.warning(self, "Rien à appliquer", "Cochez au moins une ligne.")
            return
        confirm = QMessageBox.question(
            self, "Confirmer la synchronisation",
            "Appliquer %d changement(s) de prix ?\n\n"
            "Si un magasin cible est en ligne, l'écriture sera immédiate ; "
            "sinon elle sera mise en file." % len(selected),
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        if confirm != QMessageBox.Yes:
            return
        self._apply_btn.setEnabled(False)
        self._status.setText("Envoi de %d changement(s)…" % len(selected))
        self._thread = _SyncThread(self._data, selected)
        self._thread.progress.connect(self._status.setText)
        self._thread.finished_all.connect(self._on_done)
        self._thread.start()

    def _on_done(self) -> None:
        self._apply_btn.setEnabled(True)
        self._status.setText(self._status.text() + " — terminé.")
