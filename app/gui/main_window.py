"""PySide6 GUI: configure the tool, run a sync, and toggle scheduling.

Kept intentionally simple -- this is a config/preview/log tool for one
person, not a product.
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
from app.sync.order_importer import run_order_import
from app.sync.stock_sync import run_stock_sync

try:
    from app.scheduler import windows_task
except Exception:  # pragma: no cover - only relevant off-Windows
    windows_task = None


def _parse_mapping_text(text):
    """Parses "key=value" lines (one per row) into a dict, ignoring blank
    lines and lines without an '='."""
    result = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip()
        if key and value:
            result[key] = value
    return result


def _format_mapping_text(mapping):
    return "\n".join(f"{k}={v}" for k, v in (mapping or {}).items())


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


class StockSyncWorker(QThread):
    line = Signal(str)
    finished_ok = Signal(dict)
    finished_error = Signal(str)

    def __init__(self, cfg, dry_run):
        super().__init__()
        self.cfg = cfg
        self.dry_run = dry_run

    def run(self):
        try:
            report = run_stock_sync(self.cfg, dry_run=self.dry_run, log_fn=self.line.emit)
            self.finished_ok.emit(report)
        except Exception as exc:  # noqa: BLE001
            self.finished_error.emit(str(exc))


class OrderImportWorker(QThread):
    line = Signal(str)
    finished_ok = Signal(dict)
    finished_error = Signal(str)

    def __init__(self, cfg, dry_run):
        super().__init__()
        self.cfg = cfg
        self.dry_run = dry_run

    def run(self):
        try:
            report = run_order_import(self.cfg, dry_run=self.dry_run, log_fn=self.line.emit)
            self.finished_ok.emit(report)
        except Exception as exc:  # noqa: BLE001
            self.finished_error.emit(str(exc))


class MainWindow(QMainWindow):
    def __init__(self, config_path):
        super().__init__()
        self.config_path = config_path
        self.cfg = load_config(config_path)
        self.worker = None

        self.setWindowTitle("ERP -> WooCommerce Product Sync")
        self.resize(720, 560)

        self.stock_worker = None
        self.order_worker = None

        tabs = QTabWidget()
        tabs.addTab(self._build_config_tab(), "Configuration")
        tabs.addTab(self._build_sync_tab(), "Sync")
        tabs.addTab(self._build_stock_sync_tab(), "Stock Sync")
        tabs.addTab(self._build_orders_tab(), "Orders")
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

    # -- Stock Sync tab -------------------------------------------------------
    def _build_stock_sync_tab(self):
        widget = QWidget()
        layout = QVBoxLayout(widget)

        opts_group = QGroupBox("Options")
        opts_form = QFormLayout(opts_group)
        self.stock_zero_missing = QCheckBox(
            "Zero out WooCommerce products whose SKU isn't found in the DB at all"
        )
        self.stock_zero_missing.setChecked(self.cfg["stock_sync"]["zero_missing_in_db"])
        opts_form.addRow(self.stock_zero_missing)
        layout.addWidget(opts_group)

        btn_row = QHBoxLayout()
        self.stock_dry_run_btn = QPushButton("Dry run (preview only)")
        self.stock_dry_run_btn.clicked.connect(lambda: self._start_stock_sync(dry_run=True))
        self.stock_sync_btn = QPushButton("Sync stock now")
        self.stock_sync_btn.clicked.connect(lambda: self._start_stock_sync(dry_run=False))
        btn_row.addWidget(self.stock_dry_run_btn)
        btn_row.addWidget(self.stock_sync_btn)
        layout.addLayout(btn_row)

        self.stock_log_view = QPlainTextEdit()
        self.stock_log_view.setReadOnly(True)
        layout.addWidget(self.stock_log_view)
        return widget

    def _start_stock_sync(self, dry_run):
        if self.stock_worker and self.stock_worker.isRunning():
            return
        cfg = self._collect_config()
        cfg["stock_sync"]["zero_missing_in_db"] = self.stock_zero_missing.isChecked()
        self.stock_log_view.clear()
        self.stock_dry_run_btn.setEnabled(False)
        self.stock_sync_btn.setEnabled(False)
        self.stock_worker = StockSyncWorker(cfg, dry_run)
        self.stock_worker.line.connect(self.stock_log_view.appendPlainText)
        self.stock_worker.finished_ok.connect(self._stock_sync_done)
        self.stock_worker.finished_error.connect(self._stock_sync_failed)
        self.stock_worker.start()

    def _stock_sync_done(self, report):
        self.stock_dry_run_btn.setEnabled(True)
        self.stock_sync_btn.setEnabled(True)
        if report.get("updates_preview"):
            self.stock_log_view.appendPlainText("--- Would update ---")
            for u in report["updates_preview"][:self.PAYLOAD_PREVIEW_LIMIT]:
                self.stock_log_view.appendPlainText(f"{u['sku']}: stock_quantity={u['stock_quantity']}")
        if report.get("errors"):
            self.stock_log_view.appendPlainText(f"Errors: {report['errors']}")

    def _stock_sync_failed(self, message):
        self.stock_dry_run_btn.setEnabled(True)
        self.stock_sync_btn.setEnabled(True)
        self.stock_log_view.appendPlainText(f"FAILED: {message}")

    # -- Orders tab -------------------------------------------------------------
    def _build_orders_tab(self):
        widget = QWidget()
        layout = QVBoxLayout(widget)
        oi_cfg = self.cfg["order_import"]

        cfg_group = QGroupBox("WooCommerce order -> Firebird document mapping")
        form = QFormLayout(cfg_group)
        self.order_client_code = QLineEdit(oi_cfg["client_code"])
        form.addRow("Client CODE_TIERS:", self.order_client_code)
        self.order_code_depot = QLineEdit(oi_cfg["code_depot"])
        form.addRow("Default CODE_DEPOT:", self.order_code_depot)
        self.order_username = QLineEdit(oi_cfg["username"])
        form.addRow("Username stamped on PIECE:", self.order_username)
        self.order_on_missing_sku = QComboBox()
        self.order_on_missing_sku.addItems(["skip_line", "skip_order"])
        self.order_on_missing_sku.setCurrentText(oi_cfg["on_missing_sku"])
        form.addRow("On missing SKU:", self.order_on_missing_sku)
        layout.addWidget(cfg_group)

        mapping_group = QGroupBox("Status mapping (one 'wc_status=CODE_TYPE_PIECE' per line)")
        mapping_layout = QVBoxLayout(mapping_group)
        self.order_status_mapping = QPlainTextEdit(_format_mapping_text(oi_cfg["status_mapping"]))
        self.order_status_mapping.setMaximumHeight(80)
        mapping_layout.addWidget(self.order_status_mapping)
        layout.addWidget(mapping_group)

        transform_group = QGroupBox("Transformation linking (one 'TARGET_TYPE=SOURCE_TYPE' per line)")
        transform_layout = QVBoxLayout(transform_group)
        self.order_transformation = QPlainTextEdit(_format_mapping_text(oi_cfg["transformation"]))
        self.order_transformation.setMaximumHeight(60)
        transform_layout.addWidget(self.order_transformation)
        layout.addWidget(transform_group)

        btn_row = QHBoxLayout()
        self.order_dry_run_btn = QPushButton("Dry run (preview only)")
        self.order_dry_run_btn.clicked.connect(lambda: self._start_order_import(dry_run=True))
        self.order_import_btn = QPushButton("Import orders now")
        self.order_import_btn.clicked.connect(lambda: self._start_order_import(dry_run=False))
        btn_row.addWidget(self.order_dry_run_btn)
        btn_row.addWidget(self.order_import_btn)
        layout.addLayout(btn_row)

        self.order_log_view = QPlainTextEdit()
        self.order_log_view.setReadOnly(True)
        layout.addWidget(self.order_log_view)
        return widget

    def _collect_order_import_config(self):
        oi_cfg = self.cfg["order_import"]
        oi_cfg["client_code"] = self.order_client_code.text()
        oi_cfg["code_depot"] = self.order_code_depot.text()
        oi_cfg["username"] = self.order_username.text()
        oi_cfg["on_missing_sku"] = self.order_on_missing_sku.currentText()
        oi_cfg["status_mapping"] = _parse_mapping_text(self.order_status_mapping.toPlainText())
        oi_cfg["transformation"] = _parse_mapping_text(self.order_transformation.toPlainText())
        return oi_cfg

    def _start_order_import(self, dry_run):
        if self.order_worker and self.order_worker.isRunning():
            return
        cfg = self._collect_config()
        self._collect_order_import_config()
        self.order_log_view.clear()
        self.order_dry_run_btn.setEnabled(False)
        self.order_import_btn.setEnabled(False)
        self.order_worker = OrderImportWorker(cfg, dry_run)
        self.order_worker.line.connect(self.order_log_view.appendPlainText)
        self.order_worker.finished_ok.connect(self._order_import_done)
        self.order_worker.finished_error.connect(self._order_import_failed)
        self.order_worker.start()

    def _order_import_done(self, report):
        self.order_dry_run_btn.setEnabled(True)
        self.order_import_btn.setEnabled(True)
        self.order_log_view.appendPlainText(
            f"created={len(report['created'])} skipped={len(report['skipped'])} "
            f"errors={len(report['errors'])}"
        )
        if report.get("errors"):
            self.order_log_view.appendPlainText(f"Errors: {report['errors']}")

    def _order_import_failed(self, message):
        self.order_dry_run_btn.setEnabled(True)
        self.order_import_btn.setEnabled(True)
        self.order_log_view.appendPlainText(f"FAILED: {message}")

    # -- Schedule tab -------------------------------------------------------
    # Each mode gets its own independent Task Scheduler entry, since they
    # run at very different cadences (stock changes constantly; full
    # product/order syncs don't need to run nearly as often).
    _SCHEDULE_MODES = [
        # (attr_prefix, group title, cli_flag, task_name, config_section, label)
        ("schedule", "Product Sync", "--sync", windows_task.TASK_NAME_SYNC if windows_task else None,
         "schedule", "Every N minutes:"),
        ("stock_schedule", "Stock Sync", "--stock-sync",
         windows_task.TASK_NAME_STOCK_SYNC if windows_task else None,
         "stock_sync", "Every N minutes:"),
        ("order_schedule", "Order Import", "--import-orders",
         windows_task.TASK_NAME_ORDER_IMPORT if windows_task else None,
         "order_import", "Every N minutes:"),
    ]

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

        for attr_prefix, title, cli_flag, task_name, section, label in self._SCHEDULE_MODES:
            group = QGroupBox(title)
            group_layout = QVBoxLayout(group)

            enabled_box = QCheckBox("Enabled")
            enabled_box.setChecked(self.cfg[section]["enabled"])
            setattr(self, f"{attr_prefix}_enabled", enabled_box)
            group_layout.addWidget(enabled_box)

            form = QFormLayout()
            interval_box = QSpinBox()
            interval_box.setRange(1, 10080)  # up to a week, in minutes
            interval_box.setValue(self.cfg[section]["interval_minutes"])
            setattr(self, f"{attr_prefix}_interval", interval_box)
            form.addRow(label, interval_box)
            group_layout.addLayout(form)
            layout.addWidget(group)

        apply_btn = QPushButton("Apply schedules")
        apply_btn.clicked.connect(self._apply_schedule)
        layout.addWidget(apply_btn)
        layout.addStretch()
        return widget

    def _apply_schedule(self):
        cfg = self._collect_config()
        messages = []
        for attr_prefix, title, cli_flag, task_name, section, _label in self._SCHEDULE_MODES:
            enabled = getattr(self, f"{attr_prefix}_enabled").isChecked()
            interval = getattr(self, f"{attr_prefix}_interval").value()
            cfg[section]["enabled"] = enabled
            cfg[section]["interval_minutes"] = interval
            try:
                if enabled:
                    windows_task.install(sys.executable, cli_flag, interval, task_name)
                    messages.append(f"{title}: scheduled every {interval} min")
                else:
                    windows_task.remove(task_name)
                    messages.append(f"{title}: removed")
            except Exception as exc:  # noqa: BLE001
                messages.append(f"{title}: FAILED ({exc})")
        save_config(self.config_path, cfg)
        QMessageBox.information(self, "Schedules updated", "\n".join(messages))


def launch(config_path):
    app = QApplication(sys.argv)
    window = MainWindow(config_path)
    window.show()
    return app.exec()
