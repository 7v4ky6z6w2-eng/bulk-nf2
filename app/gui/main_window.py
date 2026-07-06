"""PySide6 GUI: configure the tool, run a sync, and toggle scheduling.

Kept intentionally simple (three tabs) -- this is a config/preview/log tool
for one person, not a product.
"""

import platform
import sys

from PySide6.QtCore import QThread, Signal
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QFileDialog, QFormLayout, QGroupBox,
    QHBoxLayout, QLabel, QLineEdit, QMainWindow, QMessageBox, QPlainTextEdit,
    QPushButton, QSpinBox, QTabWidget, QVBoxLayout, QWidget,
)

from app.config import load_config, save_config
from app.sync.engine import render_report_lines, run_sync, write_dry_run_payloads

try:
    from app.scheduler import windows_task
except Exception:  # pragma: no cover - only relevant off-Windows
    windows_task = None


class SyncWorker(QThread):
    line = Signal(str)
    finished_ok = Signal(dict)
    finished_error = Signal(str)

    def __init__(self, cfg, dry_run):
        super().__init__()
        self.cfg = cfg
        self.dry_run = dry_run

    def run(self):
        try:
            report = run_sync(self.cfg, dry_run=self.dry_run, log_fn=self.line.emit)
            self.finished_ok.emit(report)
        except Exception as exc:  # noqa: BLE001 - surface any failure to the GUI
            self.finished_error.emit(str(exc))


class MainWindow(QMainWindow):
    def __init__(self, config_path):
        super().__init__()
        self.config_path = config_path
        self.cfg = load_config(config_path)
        self.worker = None

        self.setWindowTitle("ERP -> WooCommerce Product Sync")
        self.resize(720, 560)

        tabs = QTabWidget()
        tabs.addTab(self._build_config_tab(), "Configuration")
        tabs.addTab(self._build_sync_tab(), "Sync")
        tabs.addTab(self._build_schedule_tab(), "Schedule")
        self.setCentralWidget(tabs)

    # -- Configuration tab ----------------------------------------------------
    def _build_config_tab(self):
        widget = QWidget()
        layout = QVBoxLayout(widget)

        fb_group = QGroupBox("Firebird database (.FDB)")
        fb_form = QFormLayout(fb_group)
        self.db_path = QLineEdit(self.cfg["firebird"]["database"])
        browse_btn = QPushButton("Browse...")
        browse_btn.clicked.connect(self._browse_fdb)
        db_row = QHBoxLayout()
        db_row.addWidget(self.db_path)
        db_row.addWidget(browse_btn)
        db_row_widget = QWidget()
        db_row_widget.setLayout(db_row)
        fb_form.addRow("Database path:", db_row_widget)
        self.fb_host = QLineEdit(self.cfg["firebird"]["host"])
        fb_form.addRow("Host (blank = local file):", self.fb_host)
        self.fb_port = QSpinBox()
        self.fb_port.setRange(1, 65535)
        self.fb_port.setValue(self.cfg["firebird"]["port"])
        fb_form.addRow("Port:", self.fb_port)
        self.fb_user = QLineEdit(self.cfg["firebird"]["user"])
        fb_form.addRow("User:", self.fb_user)
        self.fb_password = QLineEdit(self.cfg["firebird"]["password"])
        self.fb_password.setEchoMode(QLineEdit.Password)
        fb_form.addRow("Password:", self.fb_password)
        self.fb_charset = QLineEdit(self.cfg["firebird"]["charset"])
        fb_form.addRow("Charset:", self.fb_charset)
        layout.addWidget(fb_group)

        sync_group = QGroupBox("Sync scope")
        sync_form = QFormLayout(sync_group)
        self.filter_boutique = QCheckBox("Only sync articles whose family is BOUTIQ_VISIBLE")
        self.filter_boutique.setChecked(self.cfg["sync"]["filter_boutique_visible"])
        sync_form.addRow(self.filter_boutique)
        self.price_field = QComboBox()
        self.price_field.addItems(["PRIXVENTETTC", "PRIXVENTEHT"])
        self.price_field.setCurrentText(self.cfg["sync"]["price_field"])
        sync_form.addRow("Regular price source:", self.price_field)
        self.sync_images = QCheckBox("Use ARTICLE.PHOTO when it's a real image")
        self.sync_images.setChecked(self.cfg["sync"]["sync_images"])
        sync_form.addRow(self.sync_images)
        layout.addWidget(sync_group)

        wc_group = QGroupBox("WooCommerce REST API")
        wc_form = QFormLayout(wc_group)
        self.wc_url = QLineEdit(self.cfg["woocommerce"]["site_url"])
        wc_form.addRow("Site URL:", self.wc_url)
        self.wc_key = QLineEdit(self.cfg["woocommerce"]["consumer_key"])
        wc_form.addRow("Consumer key:", self.wc_key)
        self.wc_secret = QLineEdit(self.cfg["woocommerce"]["consumer_secret"])
        self.wc_secret.setEchoMode(QLineEdit.Password)
        wc_form.addRow("Consumer secret:", self.wc_secret)
        layout.addWidget(wc_group)

        wp_group = QGroupBox("WordPress (only needed to upload ARTICLE.PHOTO images)")
        wp_form = QFormLayout(wp_group)
        self.wp_user = QLineEdit(self.cfg["wordpress"]["username"])
        wp_form.addRow("Username:", self.wp_user)
        self.wp_pass = QLineEdit(self.cfg["wordpress"]["app_password"])
        self.wp_pass.setEchoMode(QLineEdit.Password)
        wp_form.addRow("Application password:", self.wp_pass)
        layout.addWidget(wp_group)

        save_btn = QPushButton("Save configuration")
        save_btn.clicked.connect(self._save_config)
        layout.addWidget(save_btn)
        layout.addStretch()
        return widget

    def _browse_fdb(self):
        path, _ = QFileDialog.getOpenFileName(self, "Select .FDB file", "", "Firebird DB (*.fdb *.FDB)")
        if path:
            self.db_path.setText(path)

    def _collect_config(self):
        self.cfg["firebird"]["database"] = self.db_path.text()
        self.cfg["firebird"]["host"] = self.fb_host.text()
        self.cfg["firebird"]["port"] = self.fb_port.value()
        self.cfg["firebird"]["user"] = self.fb_user.text()
        self.cfg["firebird"]["password"] = self.fb_password.text()
        self.cfg["firebird"]["charset"] = self.fb_charset.text()
        self.cfg["sync"]["filter_boutique_visible"] = self.filter_boutique.isChecked()
        self.cfg["sync"]["price_field"] = self.price_field.currentText()
        self.cfg["sync"]["sync_images"] = self.sync_images.isChecked()
        self.cfg["woocommerce"]["site_url"] = self.wc_url.text()
        self.cfg["woocommerce"]["consumer_key"] = self.wc_key.text()
        self.cfg["woocommerce"]["consumer_secret"] = self.wc_secret.text()
        self.cfg["wordpress"]["username"] = self.wp_user.text()
        self.cfg["wordpress"]["app_password"] = self.wp_pass.text()
        return self.cfg

    def _save_config(self):
        cfg = self._collect_config()
        save_config(self.config_path, cfg)
        QMessageBox.information(self, "Saved", f"Configuration saved to {self.config_path}")

    # -- Sync tab ---------------------------------------------------------------
    def _build_sync_tab(self):
        widget = QWidget()
        layout = QVBoxLayout(widget)

        btn_row = QHBoxLayout()
        self.dry_run_btn = QPushButton("Dry run (preview only)")
        self.dry_run_btn.clicked.connect(lambda: self._start_sync(dry_run=True))
        self.sync_btn = QPushButton("Sync now")
        self.sync_btn.clicked.connect(lambda: self._start_sync(dry_run=False))
        btn_row.addWidget(self.dry_run_btn)
        btn_row.addWidget(self.sync_btn)
        layout.addLayout(btn_row)

        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        layout.addWidget(self.log_view)
        return widget

    def _start_sync(self, dry_run):
        if self.worker and self.worker.isRunning():
            return
        cfg = self._collect_config()
        self.log_view.clear()
        self.dry_run_btn.setEnabled(False)
        self.sync_btn.setEnabled(False)
        self.worker = SyncWorker(cfg, dry_run)
        self.worker.line.connect(self.log_view.appendPlainText)
        self.worker.finished_ok.connect(self._sync_done)
        self.worker.finished_error.connect(self._sync_failed)
        self.worker.start()

    # Catalogs can run into the thousands of articles -- capped so the log
    # view (and Qt) stay responsive; the full list still goes to disk.
    PAYLOAD_PREVIEW_LIMIT = 200

    def _sync_done(self, report):
        self.dry_run_btn.setEnabled(True)
        self.sync_btn.setEnabled(True)
        for line in render_report_lines(report, payload_limit=self.PAYLOAD_PREVIEW_LIMIT):
            self.log_view.appendPlainText(line)
        written_path = write_dry_run_payloads(report)
        if written_path:
            self.log_view.appendPlainText(
                f"\nFull list of {len(report['payloads'])} payload(s) written to {written_path}"
            )

    def _sync_failed(self, message):
        self.dry_run_btn.setEnabled(True)
        self.sync_btn.setEnabled(True)
        self.log_view.appendPlainText(f"FAILED: {message}")

    # -- Schedule tab -------------------------------------------------------
    def _build_schedule_tab(self):
        widget = QWidget()
        layout = QVBoxLayout(widget)

        if platform.system() != "Windows":
            layout.addWidget(QLabel(
                "Scheduling uses Windows Task Scheduler and is only available "
                "when running the packaged .exe on Windows."
            ))
            layout.addStretch()
            return widget

        self.schedule_enabled = QCheckBox("Run sync automatically on a schedule")
        self.schedule_enabled.setChecked(self.cfg["schedule"]["enabled"])
        layout.addWidget(self.schedule_enabled)

        form = QFormLayout()
        self.schedule_interval = QSpinBox()
        self.schedule_interval.setRange(1, 168)
        self.schedule_interval.setValue(self.cfg["schedule"]["interval_hours"])
        form.addRow("Every N hours:", self.schedule_interval)
        layout.addLayout(form)

        apply_btn = QPushButton("Apply schedule")
        apply_btn.clicked.connect(self._apply_schedule)
        layout.addWidget(apply_btn)
        layout.addStretch()
        return widget

    def _apply_schedule(self):
        cfg = self._collect_config()
        cfg["schedule"]["enabled"] = self.schedule_enabled.isChecked()
        cfg["schedule"]["interval_hours"] = self.schedule_interval.value()
        save_config(self.config_path, cfg)
        try:
            if cfg["schedule"]["enabled"]:
                windows_task.install(sys.executable, cfg["schedule"]["interval_hours"])
                QMessageBox.information(self, "Scheduled", "Automatic sync scheduled.")
            else:
                windows_task.remove()
                QMessageBox.information(self, "Unscheduled", "Automatic sync removed.")
        except Exception as exc:  # noqa: BLE001
            QMessageBox.critical(self, "Scheduling failed", str(exc))


def launch(config_path):
    app = QApplication(sys.argv)
    window = MainWindow(config_path)
    window.show()
    return app.exec()
