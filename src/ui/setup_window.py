"""Assistant de première configuration / réglages.

Permet de saisir les informations de connexion au serveur Firebird et au site
WooCommerce, de les tester, puis de les enregistrer dans config.ini.
"""

from __future__ import annotations

from PySide2.QtCore import Qt
from PySide2.QtWidgets import (
    QDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
)

import i18n
from config import AppConfig, FirebirdConfig, WooConfig, save
from database import Database
from woocommerce import WooClient


class SetupDialog(QDialog):
    def __init__(self, cfg: AppConfig, parent=None):
        super().__init__(parent)
        self._cfg = cfg
        self.setWindowTitle(i18n.SETUP_TITLE)
        self.setMinimumWidth(520)
        self._build_ui()
        self._load_values()

    # --- construction de l'interface --------------------------------------
    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)

        intro = QLabel(
            "Renseignez la connexion au serveur Netfact (base de données) "
            "puis, si besoin, votre site WooCommerce pour les photos."
        )
        intro.setWordWrap(True)
        layout.addWidget(intro)

        # --- Serveur / base de données
        fb_box = QGroupBox("Serveur Netfact (base de données)")
        fb_form = QFormLayout(fb_box)
        self.host = QLineEdit()
        self.host.setPlaceholderText("ex : SERVEUR ou 192.168.1.10")
        self.port = QSpinBox()
        self.port.setRange(1, 65535)
        self.port.setValue(3050)
        self.database = QLineEdit()
        self.database.setPlaceholderText(r"ex : C:\Netfact\Data\PR22.FDB")
        self.user = QLineEdit("SYSDBA")
        self.password = QLineEdit()
        self.password.setEchoMode(QLineEdit.Password)
        fb_form.addRow(i18n.SETUP_SERVER, self.host)
        fb_form.addRow(i18n.SETUP_PORT, self.port)
        fb_form.addRow(i18n.SETUP_DATABASE, self.database)
        fb_form.addRow(i18n.SETUP_USER, self.user)
        fb_form.addRow(i18n.SETUP_PASSWORD, self.password)
        layout.addWidget(fb_box)

        # --- WooCommerce
        woo_box = QGroupBox("Site WooCommerce (photos — optionnel)")
        woo_form = QFormLayout(woo_box)
        self.woo_url = QLineEdit()
        self.woo_url.setPlaceholderText("ex : https://votresite.com")
        self.woo_key = QLineEdit()
        self.woo_secret = QLineEdit()
        self.woo_secret.setEchoMode(QLineEdit.Password)
        woo_form.addRow(i18n.SETUP_WOO_URL, self.woo_url)
        woo_form.addRow(i18n.SETUP_WOO_KEY, self.woo_key)
        woo_form.addRow(i18n.SETUP_WOO_SECRET, self.woo_secret)
        layout.addWidget(woo_box)

        # --- boutons
        btns = QHBoxLayout()
        self.test_btn = QPushButton(i18n.SETUP_TEST)
        self.test_btn.clicked.connect(self._on_test)
        self.save_btn = QPushButton(i18n.SETUP_SAVE)
        self.save_btn.setDefault(True)
        self.save_btn.clicked.connect(self._on_save)
        btns.addWidget(self.test_btn)
        btns.addStretch(1)
        btns.addWidget(self.save_btn)
        layout.addLayout(btns)

    def _load_values(self) -> None:
        fb = self._cfg.firebird
        self.host.setText(fb.host)
        self.port.setValue(fb.port or 3050)
        self.database.setText(fb.database)
        self.user.setText(fb.user or "SYSDBA")
        self.password.setText(fb.password)
        woo = self._cfg.woocommerce
        self.woo_url.setText(woo.base_url)
        self.woo_key.setText(woo.consumer_key)
        self.woo_secret.setText(woo.consumer_secret)

    # --- collecte des valeurs ---------------------------------------------
    def _collect(self) -> AppConfig:
        cfg = self._cfg
        cfg.firebird = FirebirdConfig(
            host=self.host.text().strip(),
            port=self.port.value(),
            database=self.database.text().strip(),
            user=self.user.text().strip() or "SYSDBA",
            password=self.password.text(),
        )
        cfg.woocommerce = WooConfig(
            base_url=self.woo_url.text().strip().rstrip("/"),
            consumer_key=self.woo_key.text().strip(),
            consumer_secret=self.woo_secret.text().strip(),
        )
        return cfg

    # --- actions -----------------------------------------------------------
    def _on_test(self) -> None:
        cfg = self._collect()
        messages = []
        ok = True

        # Test base de données
        db = Database(cfg.firebird)
        try:
            db.test_connection()
            messages.append("✅ Base de données : " + i18n.SETUP_TEST_OK)
        except Exception as exc:
            ok = False
            messages.append(f"❌ Base de données : {exc}")
        finally:
            db.close()

        # Test WooCommerce (seulement si renseigné)
        woo = WooClient(cfg.woocommerce)
        if woo.configured:
            try:
                woo.test()
                messages.append("✅ WooCommerce : " + i18n.SETUP_TEST_OK)
            except Exception as exc:
                messages.append(f"⚠️ WooCommerce : {exc}")
        else:
            messages.append("ℹ️ WooCommerce non configuré (photos désactivées)")

        box = QMessageBox(self)
        box.setWindowTitle(i18n.SETUP_TEST_OK if ok else i18n.SETUP_TEST_FAIL)
        box.setIcon(QMessageBox.Information if ok else QMessageBox.Warning)
        box.setText("\n".join(messages))
        box.exec()

    def _on_save(self) -> None:
        cfg = self._collect()
        if not cfg.is_complete():
            QMessageBox.warning(
                self,
                "Champs manquants",
                "Veuillez renseigner le chemin de la base, "
                "l'utilisateur et le mot de passe.",
            )
            return
        save(cfg)
        self.accept()
