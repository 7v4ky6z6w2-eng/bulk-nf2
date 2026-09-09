"""Dialogue de connexion : URL du hub + code d'accès.

Permet d'utiliser l'application sur n'importe quel appareil sans copier
stores.json : on saisit l'adresse du magasin 1 et le code d'accès, l'app
récupère ensuite la liste des magasins depuis le hub.
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog, QDialogButtonBox, QFormLayout, QLabel, QLineEdit, QVBoxLayout,
)

from desktop.hub_data import HubData


class LoginDialog(QDialog):
    def __init__(self, hub_url: str = "", code: str = "", parent=None):
        super().__init__(parent)
        self.setWindowTitle("Connexion au hub PrimeNF")
        self.setMinimumWidth(420)
        self.hub_url = ""
        self.code = ""
        self.data: HubData | None = None
        self.stores: list = []

        self._url = QLineEdit(hub_url)
        self._url.setPlaceholderText("http://100.x.y.z:5000")
        self._code = QLineEdit(code)
        self._code.setEchoMode(QLineEdit.Password)
        self._code.setPlaceholderText("Code d'accès")
        self._msg = QLabel()
        self._msg.setWordWrap(True)
        self._msg.setStyleSheet("color:#dc3545;")

        form = QFormLayout()
        form.addRow("Adresse du hub :", self._url)
        form.addRow("Code d'accès :", self._code)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Ok).setText("Se connecter")
        buttons.accepted.connect(self._try_connect)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("<b>Connectez-vous au magasin 1 (hub)</b>"))
        layout.addWidget(QLabel("Saisissez l'adresse du hub et votre code d'accès. "
                                "La liste des magasins sera récupérée automatiquement."))
        layout.addLayout(form)
        layout.addWidget(self._msg)
        layout.addWidget(buttons)

    def _try_connect(self) -> None:
        url = self._url.text().strip().rstrip("/")
        code = self._code.text().strip()
        if not url:
            self._msg.setText("Indiquez l'adresse du hub.")
            return
        if not url.startswith("http"):
            url = "http://" + url
        data = HubData(url, code)
        try:
            cfg = data._get("/api/client/config")  # valide URL + code
        except Exception as exc:  # noqa: BLE001
            self._msg.setText("Connexion impossible : %s" % exc)
            return
        self.hub_url = url
        self.code = code
        self.data = data
        self.stores = cfg.get("stores", [])
        self.accept()
