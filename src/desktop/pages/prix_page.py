"""Page Éditeur de prix : recherche (via hub) + soumission au hub (1 ou N magasins).

Aucun accès Firebird ici : la recherche d'articles et l'application des prix
passent par le hub. Pour chaque magasin sélectionné, le hub applique tout de
suite (en ligne) ou met en file (hors ligne). TTC = HT (pas de TVA).

Les lignes sont regroupées par `match_key` (code-barres équivalent partagé,
sinon référence) : le même produit vendu sous des références différentes selon
le magasin apparaît comme UNE ligne, et l'application cible la référence
PROPRE à chaque magasin. En plus du prix de vente, la section « Promotion »
permet d'éditer le prix promo, son activation et sa période (colonnes
PRIXHTPROMO/PRIXTTCPROMO/ACTIVEPROMO/DATEDEBPROMO/DATEFINPROMO du logiciel).
"""

from __future__ import annotations

from datetime import datetime

from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDoubleSpinBox, QFormLayout, QGroupBox,
    QHBoxLayout, QLabel, QLineEdit, QMessageBox,
    QPushButton, QTableWidget, QTableWidgetItem, QTextEdit,
    QVBoxLayout, QWidget,
)

from stores import StoreRegistry
from desktop.pages.price_sync import MatchSuggestionsDialog, PriceSyncDialog


def _parse_date_input(text: str) -> str | None:
    """'25/12/2026' ou '2026-12-25' -> '2026-12-25'. Vide -> None (efface).

    Lève ValueError si le texte n'est pas une date reconnue."""
    text = (text or "").strip()
    if not text:
        return None
    for fmt in ("%d/%m/%Y", "%Y-%m-%d", "%d-%m-%Y", "%d/%m/%y"):
        try:
            return datetime.strptime(text, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    raise ValueError("Date invalide : %r (attendu JJ/MM/AAAA)" % text)


class _PriceThread(QThread):
    progress = Signal(int, str, str)   # store_id, status, message
    finished_all = Signal()

    def __init__(self, data, changes_by_store: dict):
        super().__init__()
        self._data = data
        self._changes_by_store = changes_by_store   # {store_id: [changes]}

    def run(self) -> None:
        for sid, changes in self._changes_by_store.items():
            try:
                res = self._data.submit_op(sid, "price_update", {"changes": changes})
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
    _FILTER_ALL = 0        # tous les articles
    _FILTER_EVERY = 1      # présents dans TOUS les magasins
    # index >= 2 : présents dans le magasin d'id userData

    def __init__(self, registry: StoreRegistry, data, parent=None):
        super().__init__(parent)
        self._registry = registry
        self._data = data
        self._thread: _PriceThread | None = None
        self._groups: list = []        # groupes actuellement affichés (par ligne)
        self._all_groups: list = []    # groupes de la dernière recherche

        # ── Recherche article + filtre disponibilité ──
        self._search = QLineEdit()
        self._search.setPlaceholderText("Référence, désignation ou code-barres…")
        self._search.returnPressed.connect(self._do_search)
        search_btn = QPushButton("Chercher")
        search_btn.clicked.connect(self._do_search)

        self._filter = QComboBox()
        self._filter.addItem("Tous les articles", None)
        self._filter.addItem("Disponibles partout", "every")
        for s in registry.stores:
            self._filter.addItem("Disponibles : %s" % s.name, s.id)
        self._filter.currentIndexChanged.connect(self._render_groups)

        suggest_btn = QPushButton("Suggestions de correspondance…")
        suggest_btn.clicked.connect(self._open_suggestions)
        sync_btn = QPushButton("Synchroniser depuis un magasin…")
        sync_btn.clicked.connect(self._open_sync)

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
        self._results.itemSelectionChanged.connect(self._on_select_group)

        # ── Prix de vente ──
        self._price_check = QCheckBox("Modifier le prix de vente")
        self._price_check.setChecked(True)
        self._new_price = QDoubleSpinBox()
        self._new_price.setRange(0, 9_999_999)
        self._new_price.setDecimals(2)
        self._new_price.setSuffix(" DA")

        # ── Promotion ──
        self._promo_check = QCheckBox("Modifier la promotion")
        self._promo_price = QDoubleSpinBox()
        self._promo_price.setRange(0, 9_999_999)
        self._promo_price.setDecimals(2)
        self._promo_price.setSuffix(" DA")
        self._promo_active = QCheckBox("Promo active")
        self._promo_deb = QLineEdit()
        self._promo_deb.setPlaceholderText("JJ/MM/AAAA (vide = aucune)")
        self._promo_fin = QLineEdit()
        self._promo_fin.setPlaceholderText("JJ/MM/AAAA (vide = aucune)")
        self._promo_current = QLabel("")
        self._promo_current.setWordWrap(True)
        self._promo_current.setStyleSheet("color: #adb5bd;")

        promo_form = QFormLayout()
        promo_form.addRow(self._promo_check)
        promo_form.addRow("Prix promo (HT = TTC) :", self._promo_price)
        promo_form.addRow("", self._promo_active)
        promo_form.addRow("Début promo :", self._promo_deb)
        promo_form.addRow("Fin promo :", self._promo_fin)
        promo_form.addRow(self._promo_current)
        promo_box = QGroupBox("Promotion")
        promo_box.setLayout(promo_form)

        # ── Scope magasins ──
        self._store_checks: dict[int, QCheckBox] = {}
        scope_layout = QHBoxLayout()
        for s in registry.stores:
            cb = QCheckBox(s.name)
            cb.setChecked(True)
            self._store_checks[s.id] = cb
            scope_layout.addWidget(cb)

        self._apply_btn = QPushButton("Appliquer")
        self._apply_btn.clicked.connect(self._do_apply)

        self._log = QTextEdit()
        self._log.setReadOnly(True)
        self._log.setMaximumHeight(140)

        # ── Layout ──
        search_row = QHBoxLayout()
        search_row.addWidget(self._search, 1)
        search_row.addWidget(search_btn)
        search_row.addWidget(self._filter)

        price_form = QFormLayout()
        price_form.addRow(self._price_check)
        price_form.addRow("Nouveau prix (HT = TTC) :", self._new_price)
        price_box = QGroupBox("Prix de vente")
        price_box.setLayout(price_form)

        scope_box = QGroupBox("Appliquer sur :")
        scope_box.setLayout(scope_layout)

        action_row = QHBoxLayout()
        action_row.addStretch()
        action_row.addWidget(self._apply_btn)

        tools_row = QHBoxLayout()
        tools_row.addWidget(suggest_btn)
        tools_row.addWidget(sync_btn)
        tools_row.addStretch()

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("<h3>Éditeur de prix</h3>"))
        layout.addLayout(tools_row)
        layout.addLayout(search_row)
        layout.addWidget(self._results)
        layout.addWidget(price_box)
        layout.addWidget(promo_box)
        layout.addWidget(scope_box)
        layout.addLayout(action_row)
        layout.addWidget(QLabel("Journal :"))
        layout.addWidget(self._log)

    def _log_msg(self, msg: str) -> None:
        self._log.append(msg)

    def _open_suggestions(self) -> None:
        dlg = MatchSuggestionsDialog(self._registry, self._data, self)
        dlg.exec()
        # Une correspondance confirmée pendant le dialogue change le
        # regroupement : on relance la recherche en cours pour la refléter.
        if self._search.text().strip():
            self._do_search()

    def _open_sync(self) -> None:
        dlg = PriceSyncDialog(self._registry, self._data, self)
        dlg.exec()
        if self._search.text().strip():
            self._do_search()

    # ── recherche & regroupement ──────────────────────────────────────────
    def _do_search(self) -> None:
        try:
            rows = self._data.article_search(self._search.text().strip())
        except Exception as exc:  # noqa: BLE001
            self._log_msg("Hub injoignable : %s" % exc)
            return

        # Regroupement par match_key : le même produit (code-barres partagé)
        # vendu sous des références différentes selon le magasin devient UNE
        # ligne. refs = {store_id: référence propre à ce magasin}.
        by_key: dict[str, dict] = {}
        for r in rows:
            key = r.get("match_key") or r.get("ref_art")
            g = by_key.setdefault(key, {
                "key": key, "desig": r.get("designation"),
                "refs": {}, "prices": {}, "promos": {},
            })
            sid = r.get("store_id")
            g["refs"][sid] = r.get("ref_art")
            g["prices"][sid] = r.get("prixventeht")
            g["promos"][sid] = {
                "prix": r.get("prixttcpromo"), "active": r.get("activepromo"),
                "deb": r.get("datedebpromo"), "fin": r.get("datefinpromo"),
            }
        self._all_groups = list(by_key.values())
        self._render_groups()

    def _render_groups(self) -> None:
        mode = self._filter.currentData()
        groups = self._all_groups
        if mode == "every":
            n = len(self._store_ids_ordered)
            groups = [g for g in groups if len(g["refs"]) >= n]
        elif isinstance(mode, int):
            groups = [g for g in groups if mode in g["refs"]]
        self._groups = groups

        store_ids = self._store_ids_ordered
        self._results.setRowCount(len(groups))
        for i, g in enumerate(groups):
            # Référence affichée : celle du 1er magasin qui a l'article ; si
            # les références divergent entre magasins, toutes sont montrées.
            refs = [g["refs"][sid] for sid in store_ids if sid in g["refs"]]
            uniq = sorted(set(refs))
            ref_txt = uniq[0] if len(uniq) == 1 else " / ".join(uniq)
            self._results.setItem(i, 0, QTableWidgetItem(ref_txt))
            self._results.setItem(i, 1, QTableWidgetItem(str(g["desig"] or "")))
            for col, sid in enumerate(store_ids, start=2):
                price = g["prices"].get(sid)
                cell = QTableWidgetItem("%.2f" % float(price) if price is not None else "—")
                cell.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
                self._results.setItem(i, col, cell)

    def _on_select_group(self) -> None:
        row = self._results.currentRow()
        if row < 0 or row >= len(self._groups):
            self._promo_current.setText("")
            return
        g = self._groups[row]
        names = self._registry.names()
        parts = []
        for sid in self._store_ids_ordered:
            promo = g["promos"].get(sid)
            if not promo:
                continue
            if promo.get("prix") or promo.get("active"):
                etat = "active" if promo.get("active") else "inactive"
                periode = ""
                if promo.get("deb") or promo.get("fin"):
                    periode = " du %s au %s" % (
                        (promo.get("deb") or "?")[:10], (promo.get("fin") or "?")[:10])
                parts.append("%s : %.2f DA (%s%s)" % (
                    names.get(sid, sid), float(promo.get("prix") or 0), etat, periode))
        self._promo_current.setText(
            ("Promo actuelle — " + " ; ".join(parts)) if parts
            else "Aucune promo actuelle sur cet article.")

    # ── application ───────────────────────────────────────────────────────
    def _build_values(self) -> dict:
        """Champs Firebird à modifier selon les sections activées.

        Lève ValueError sur saisie invalide (message affichable tel quel)."""
        values: dict = {}
        if self._price_check.isChecked():
            new_ht = self._new_price.value()
            if new_ht <= 0:
                raise ValueError("Le prix de vente doit être > 0.")
            values["PRIXVENTEHT"] = new_ht
            values["PRIXVENTETTC"] = new_ht
        if self._promo_check.isChecked():
            promo = self._promo_price.value()
            if self._promo_active.isChecked() and promo <= 0:
                raise ValueError("Une promo active nécessite un prix promo > 0.")
            values["PRIXHTPROMO"] = promo
            values["PRIXTTCPROMO"] = promo
            values["ACTIVEPROMO"] = 1 if self._promo_active.isChecked() else 0
            values["DATEDEBPROMO"] = _parse_date_input(self._promo_deb.text())
            values["DATEFINPROMO"] = _parse_date_input(self._promo_fin.text())
        return values

    def _do_apply(self) -> None:
        # Un clic pendant qu'un envoi precedent tourne encore remplacerait
        # self._thread par un nouveau QThread pendant que l'ancien tourne
        # toujours -> PySide detruit l'ancien objet Python alors que le
        # thread C++ sous-jacent est actif -> crash. Le bouton desactive
        # tant que _on_all_done n'a pas confirme la fin evite ce cas,
        # meme en cliquant tres vite plusieurs fois de suite.
        if self._thread is not None and self._thread.isRunning():
            return
        row = self._results.currentRow()
        if row < 0 or row >= len(self._groups):
            QMessageBox.warning(self, "Aucune sélection", "Sélectionnez un article dans la liste.")
            return
        group = self._groups[row]

        if not self._price_check.isChecked() and not self._promo_check.isChecked():
            QMessageBox.warning(self, "Rien à modifier",
                                "Cochez « Modifier le prix de vente » et/ou "
                                "« Modifier la promotion ».")
            return
        try:
            values = self._build_values()
        except ValueError as exc:
            QMessageBox.warning(self, "Saisie invalide", str(exc))
            return

        selected = [sid for sid, cb in self._store_checks.items() if cb.isChecked()]
        if not selected:
            QMessageBox.warning(self, "Scope vide", "Sélectionnez au moins un magasin.")
            return

        # Référence PROPRE à chaque magasin ; les magasins cochés qui n'ont
        # pas l'article sont écartés (signalés dans la confirmation).
        changes_by_store: dict[int, list] = {}
        missing = []
        for sid in selected:
            ref = group["refs"].get(sid)
            if ref:
                changes_by_store[sid] = [{"ref0": ref, "values": dict(values)}]
            else:
                missing.append(self._registry.get(sid).name)
        if not changes_by_store:
            QMessageBox.warning(self, "Article absent",
                                "Aucun des magasins cochés ne possède cet article.")
            return

        desc = []
        if "PRIXVENTEHT" in values:
            desc.append("prix %.2f DA" % values["PRIXVENTEHT"])
        if "PRIXTTCPROMO" in values:
            etat = "active" if values.get("ACTIVEPROMO") else "inactive"
            promo_txt = "promo %.2f DA (%s" % (values["PRIXTTCPROMO"], etat)
            if values.get("DATEDEBPROMO") or values.get("DATEFINPROMO"):
                promo_txt += ", du %s au %s" % (values.get("DATEDEBPROMO") or "—",
                                                values.get("DATEFINPROMO") or "—")
            desc.append(promo_txt + ")")
        store_names = ", ".join(self._registry.get(sid).name for sid in changes_by_store)
        refs_txt = ", ".join(sorted({c[0]["ref0"] for c in changes_by_store.values()}))
        message = ("Appliquer %s pour l'article %s sur : %s ?" %
                   (" + ".join(desc), refs_txt, store_names))
        if missing:
            message += "\n\n(Article absent de : %s — ignoré.)" % ", ".join(missing)
        message += "\n\nSi un magasin est en ligne, ce changement sera écrit immédiatement."
        confirm = QMessageBox.question(self, "Confirmer les changements", message,
                                       QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        if confirm != QMessageBox.Yes:
            return

        self._log_msg("%s → %d magasin(s)…" % (" + ".join(desc), len(changes_by_store)))
        self._apply_btn.setEnabled(False)
        self._thread = _PriceThread(self._data, changes_by_store)
        self._thread.progress.connect(self._on_progress)
        self._thread.finished_all.connect(self._on_all_done)
        self._thread.start()

    def _on_progress(self, store_id: int, status: str, msg: str) -> None:
        tag = {"applied": "[OK] ", "queued": "[FILE] "}.get(status, "[ERREUR] ")
        self._log_msg(tag + msg)

    def _on_all_done(self) -> None:
        self._log_msg("Terminé.")
        self._apply_btn.setEnabled(True)
