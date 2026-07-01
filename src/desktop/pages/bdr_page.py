"""Page Import BDR : sélecteur de magasin + soumission au hub (direct ou file).

L'aperçu (lecture Excel/PDF) se fait localement, sans base, et s'affiche ligne
par ligne dans un tableau (lignes PDF douteuses surlignées). L'import est envoyé
au hub via HTTP avec un uid d'idempotence généré au chargement du fichier : un
retry après timeout ne peut pas produire de double import.
"""

from __future__ import annotations

import os
import sys
import uuid

from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QFileDialog, QHBoxLayout, QLabel, QMessageBox, QPushButton,
    QTableWidget, QTableWidgetItem, QTextEdit, QVBoxLayout, QWidget,
)

from stores import StoreRegistry
from desktop.store_picker import StorePicker
from desktop.format import fmt_da, fmt_qty

# Vendor path pour la lecture Excel/PDF (aucun accès Firebird ici).
_VENDOR = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))), "vendor")
_BDR_DIR = os.path.join(_VENDOR, "bdr")
if _BDR_DIR not in sys.path:
    sys.path.insert(0, _BDR_DIR)

_WARN_BG = QColor("#7a5c00")   # fond « ambre » lisible sur thème sombre


class _SubmitThread(QThread):
    done = Signal(dict, str)  # (résultat hub, message d'erreur réseau éventuel)

    def __init__(self, data, store_id: int, config: dict, lines: list, op_uid: str):
        super().__init__()
        self._data = data
        self._store_id = store_id
        self._config = config
        self._lines = lines
        self._op_uid = op_uid

    def run(self) -> None:
        try:
            res = self._data.submit_op(
                self._store_id, "bdr_import",
                {"config": self._config, "lines": self._lines},
                op_uid=self._op_uid)
            self.done.emit(res, "")
        except Exception as exc:  # noqa: BLE001
            self.done.emit({}, str(exc))


class BdrPage(QWidget):
    def __init__(self, registry: StoreRegistry, data, parent=None):
        super().__init__(parent)
        self._registry = registry
        self._data = data
        self._lines: list = []
        self._config: dict = {}
        self._excel_path: str = ""
        self._op_uid: str = ""
        self._thread: _SubmitThread | None = None

        self._picker = StorePicker(registry)
        self._excel_btn = QPushButton("Choisir fichier (Excel ou PDF)…")
        self._excel_lbl = QLabel("Aucun fichier sélectionné")
        self._preview_btn = QPushButton("Prévisualiser / Charger")
        self._preview_btn.setEnabled(False)
        self._import_btn = QPushButton("Importer")
        self._import_btn.setEnabled(False)

        # Aperçu ligne par ligne (même confort que la version mobile).
        self._preview = QTableWidget(0, 5)
        self._preview.setHorizontalHeaderLabels(
            ["Référence", "Désignation", "Qté", "Prix", "Montant"])
        self._preview.setEditTriggers(QTableWidget.NoEditTriggers)
        self._preview.setAlternatingRowColors(True)
        self._preview.horizontalHeader().setStretchLastSection(True)
        self._preview.setColumnWidth(1, 280)

        self._log = QTextEdit()
        self._log.setReadOnly(True)
        self._log.setMaximumHeight(110)

        self._excel_btn.clicked.connect(self._choose_excel)
        self._preview_btn.clicked.connect(self._load_preview)
        self._import_btn.clicked.connect(self._do_import)

        top = QHBoxLayout()
        top.addWidget(self._picker)

        file_row = QHBoxLayout()
        file_row.addWidget(self._excel_btn)
        file_row.addWidget(self._excel_lbl, 1)
        file_row.addWidget(self._preview_btn)

        action_row = QHBoxLayout()
        action_row.addStretch()
        action_row.addWidget(self._import_btn)

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("<h3>Import Bon de Réception</h3>"))
        layout.addLayout(top)
        layout.addLayout(file_row)
        layout.addWidget(self._preview, 1)
        layout.addLayout(action_row)
        layout.addWidget(QLabel("Journal :"))
        layout.addWidget(self._log)

    def _log_msg(self, msg: str) -> None:
        self._log.append(msg)

    def _choose_excel(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Choisir le fichier fournisseur (Excel ou PDF)", "",
            "Fournisseur (*.xlsx *.xlsm *.pdf);;Excel (*.xlsx *.xlsm);;PDF (*.pdf);;Tous (*)")
        if path:
            self._excel_path = path
            self._excel_lbl.setText(os.path.basename(path))
            self._preview_btn.setEnabled(True)
            self._import_btn.setEnabled(False)
            self._lines = []
            self._preview.setRowCount(0)
            self._log_msg("Fichier : %s" % path)

    def _build_config(self, bdr) -> dict:
        # Config par défaut complète (colonnes/alias, TVA, arrondi…) : read_excel
        # / read_pdf en ont besoin (cfg["colonnes"] notamment). La connexion
        # réelle est résolue côté hub, qui force host/db/identifiants.
        return bdr.load_config(None)

    def _load_preview(self) -> None:
        """Lit Excel ou PDF localement (sans base) et remplit le tableau d'aperçu."""
        try:
            import import_bon_reception as bdr  # type: ignore
            cfg = self._build_config(bdr)
            is_pdf = self._excel_path.lower().endswith(".pdf")
            if is_pdf:
                self._log_msg("Lecture du PDF en cours (reconstruction glyphes)…")
            self._lines = (bdr.read_pdf(self._excel_path, cfg) if is_pdf
                           else bdr.read_excel(self._excel_path, cfg))
            self._config = cfg
            # Nouvel uid d'idempotence par fichier chargé : re-cliquer Importer
            # après un timeout ré-envoie le MÊME uid (pas de double import) ;
            # recharger un autre fichier régénère l'uid.
            self._op_uid = uuid.uuid4().hex

            self._fill_preview()
            bad = sum(1 for l in self._lines if l.get("recon") is False)
            if bad:
                self._log_msg("⚠ %d ligne(s) avec Qté × Prix ≠ Montant "
                              "(surlignées) — vérifier." % bad)
            self._log_msg("Chargé : %d articles" % len(self._lines))
            self._import_btn.setEnabled(bool(self._lines))
        except Exception as exc:  # noqa: BLE001
            self._log_msg("Erreur chargement : %s" % exc)
            self._import_btn.setEnabled(False)

    def _fill_preview(self) -> None:
        self._preview.setRowCount(len(self._lines))
        for i, l in enumerate(self._lines):
            qte = float(l.get("qte") or 0)
            prix = float(l.get("prix") or 0)
            montant = float(l.get("montant") or (qte * prix))
            cells = [
                QTableWidgetItem(str(l.get("ref_art") or "")),
                QTableWidgetItem(str(l.get("designation") or "")),
                QTableWidgetItem(fmt_qty(qte)),
                QTableWidgetItem(fmt_da(prix, suffix="")),
                QTableWidgetItem(fmt_da(montant, suffix="")),
            ]
            for col in (2, 3, 4):
                cells[col].setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
            suspicious = l.get("recon") is False
            for col, item in enumerate(cells):
                if suspicious:
                    item.setBackground(_WARN_BG)
                self._preview.setItem(i, col, item)

    def _do_import(self) -> None:
        if not self._lines:
            return
        sid = self._picker.current_id() or 0
        store = self._registry.get(sid)
        self._log_msg("Envoi au hub pour le magasin %s…" % store.name)
        self._import_btn.setEnabled(False)
        self._thread = _SubmitThread(self._data, sid, self._config,
                                     self._lines, self._op_uid)
        self._thread.done.connect(self._on_done)
        self._thread.start()

    def _on_done(self, res: dict, net_err: str) -> None:
        self._import_btn.setEnabled(True)
        if net_err:
            self._log_msg("Hub injoignable : %s" % net_err)
            QMessageBox.critical(self, "Hub injoignable",
                "Impossible de joindre le hub :\n%s\n\nVérifiez que le magasin 1 "
                "est allumé et joignable (Tailscale).\n\nVous pouvez re-cliquer "
                "Importer sans risque : le hub ignore les doublons du même envoi."
                % net_err)
            return
        status = res.get("status")
        if status == "applied":
            msg = "Import appliqué directement (%s lignes)." % res.get("count", "?")
            self._log_msg(msg)
            QMessageBox.information(self, "Succès", msg)
        elif status == "queued":
            msg = ("Magasin hors ligne — mis en file (op #%s).\n"
                   "Il sera appliqué au prochain démarrage du magasin, "
                   "avec notification." % res.get("op_id", "?"))
            self._log_msg(msg)
            QMessageBox.information(self, "En file", msg)
        else:
            err = res.get("error", "erreur inconnue")
            self._log_msg("Échec : %s" % err)
            QMessageBox.critical(self, "Échec", err)
