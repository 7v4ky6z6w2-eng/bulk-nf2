"""PySide6 GUI: configure the tool, run a sync, and toggle scheduling.

Kept intentionally simple -- this is a config/preview/log tool for one
person, not a product.
"""

import platform
import sys

from PySide6.QtCore import QDate, QThread, Signal
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QCheckBox, QComboBox, QDateEdit,
    QFileDialog, QFormLayout, QGroupBox, QHBoxLayout, QHeaderView, QLabel,
    QLineEdit, QMainWindow, QMessageBox, QPlainTextEdit, QPushButton,
    QSpinBox, QTableWidget, QTabWidget, QVBoxLayout, QWidget,
)

from app import diagnostics
from app.config import load_config, save_config
from app.sync.engine import render_report_lines, run_sync, write_dry_run_payloads
from app.sync.order_importer import fix_duplicate_orders, run_order_import
from app.sync.stock_sync import run_stock_sync

try:
    from app.scheduler import windows_task
except Exception:  # pragma: no cover - only relevant off-Windows
    windows_task = None

# Statuses WooCommerce ships with out of the box; editable comboboxes still
# accept any custom status a store adds.
_COMMON_WC_STATUSES = [
    "pending", "processing", "on-hold", "completed",
    "cancelled", "refunded", "failed",
]


def _combo_value(combo):
    """Reads a code out of an editable QComboBox: prefers the selected
    item's stored data, falls back to parsing typed "CODE - Label" or
    "CODE" text."""
    data = combo.currentData()
    if data:
        return data
    text = combo.currentText().strip()
    if " - " in text:
        return text.split(" - ", 1)[0].strip()
    return text


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


class AdoptWorker(QThread):
    line = Signal(str)
    finished_ok = Signal(dict)
    finished_error = Signal(str)

    def __init__(self, cfg):
        super().__init__()
        self.cfg = cfg

    def run(self):
        try:
            from app.sync.reconcile import run_adopt
            report = run_adopt(self.cfg, log_fn=self.line.emit)
            self.finished_ok.emit(report)
        except Exception as exc:  # noqa: BLE001
            self.finished_error.emit(str(exc))


class TestConnectionWorker(QThread):
    finished_ok = Signal(dict)
    finished_error = Signal(str)

    def __init__(self, cfg):
        super().__init__()
        self.cfg = cfg

    def run(self):
        try:
            result = diagnostics.test_connections(self.cfg)
            self.finished_ok.emit(result)
        except Exception as exc:  # noqa: BLE001
            self.finished_error.emit(str(exc))


class TypePiecesWorker(QThread):
    """Loads LOCAL_TYPE_PIECE codes/labels from Firebird in the background
    so the Orders tab's dropdowns can offer real document types instead of
    requiring the user to know/type the raw codes."""
    finished_ok = Signal(list)
    finished_error = Signal(str)

    def __init__(self, cfg):
        super().__init__()
        self.cfg = cfg

    def run(self):
        try:
            from app.db import queries
            from app.db.firebird_client import connect as connect_firebird
            con = connect_firebird(self.cfg)
            try:
                rows = queries.fetch_type_pieces(con)
            finally:
                con.close()
            self.finished_ok.emit(rows)
        except Exception as exc:  # noqa: BLE001
            self.finished_error.emit(str(exc))


class OrderDiagnosticsWorker(QThread):
    """Runs one of app.diagnostics' PIECE-table checks in the background --
    the user only has NetFact2, not a raw SQL tool, so these buttons are
    the only way for them to see this data."""
    finished_ok = Signal(str, list)
    finished_error = Signal(str, str)

    def __init__(self, cfg, kind):
        super().__init__()
        self.cfg = cfg
        self.kind = kind

    def run(self):
        try:
            if self.kind == "annulee":
                rows = diagnostics.check_piece_annulee(self.cfg)
            else:
                rows = diagnostics.check_duplicate_wc_orders(self.cfg)
            self.finished_ok.emit(self.kind, rows)
        except Exception as exc:  # noqa: BLE001
            self.finished_error.emit(self.kind, str(exc))


class FixDuplicatesWorker(QThread):
    line = Signal(str)
    finished_ok = Signal(dict)
    finished_error = Signal(str)

    def __init__(self, cfg, dry_run):
        super().__init__()
        self.cfg = cfg
        self.dry_run = dry_run

    def run(self):
        try:
            report = fix_duplicate_orders(self.cfg, dry_run=self.dry_run, log_fn=self.line.emit)
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
        self.resize(920, 720)

        self.stock_worker = None
        self.order_worker = None
        self.adopt_worker = None
        self.test_conn_worker = None
        self.type_pieces_worker = None
        self.type_piece_choices = []
        self.order_diag_worker = None
        self.fix_dup_worker = None

        tabs = QTabWidget()
        tabs.addTab(self._build_config_tab(), "Configuration")
        tabs.addTab(self._build_sync_tab(), "Sync")
        tabs.addTab(self._build_stock_sync_tab(), "Stock Sync")
        tabs.addTab(self._build_orders_tab(), "Orders")
        tabs.addTab(self._build_schedule_tab(), "Schedule")
        self.setCentralWidget(tabs)
        self.statusBar().showMessage("Ready.")

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
        self.db_path.setToolTip("Path to the .FDB file, e.g. C:\\Prime\\DIFA2.FDB")
        fb_form.addRow("Database path:", db_row_widget)
        self.fb_host = QLineEdit(self.cfg["firebird"]["host"])
        self.fb_host.setToolTip("Leave blank to open the .FDB file directly (embedded);\nset this only if connecting to a Firebird server over the network.")
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
        self.auto_sale_on_price_drop = QCheckBox(
            "If a price drops in NetFact2, show it as a WooCommerce sale price"
        )
        self.auto_sale_on_price_drop.setChecked(self.cfg["sync"]["auto_sale_on_price_drop"])
        self.auto_sale_on_price_drop.setToolTip(
            "Keeps the last-synced (higher) price as the WooCommerce regular\n"
            "price and pushes the new, lower NetFact2 price as sale_price --\n"
            "shows as a strikethrough discount instead of just changing the\n"
            "base price. When the price rises back up, it becomes the new\n"
            "regular price and the sale is cleared. An explicit ACTIVEPROMO\n"
            "in NetFact2 always takes priority over this."
        )
        sync_form.addRow(self.auto_sale_on_price_drop)
        layout.addWidget(sync_group)

        wc_group = QGroupBox("WooCommerce REST API")
        wc_form = QFormLayout(wc_group)
        self.wc_url = QLineEdit(self.cfg["woocommerce"]["site_url"])
        self.wc_url.setToolTip("e.g. https://your-store.com")
        wc_form.addRow("Site URL:", self.wc_url)
        self.wc_key = QLineEdit(self.cfg["woocommerce"]["consumer_key"])
        wc_form.addRow("Consumer key:", self.wc_key)
        self.wc_secret = QLineEdit(self.cfg["woocommerce"]["consumer_secret"])
        self.wc_secret.setEchoMode(QLineEdit.Password)
        wc_form.addRow("Consumer secret:", self.wc_secret)
        wc_form.addRow(QLabel(
            "WooCommerce -> Settings -> Advanced -> REST API -> Add key\n"
            "(needs Read/Write permissions)."
        ))
        layout.addWidget(wc_group)

        wp_group = QGroupBox("WordPress (only needed to upload ARTICLE.PHOTO images)")
        wp_form = QFormLayout(wp_group)
        self.wp_user = QLineEdit(self.cfg["wordpress"]["username"])
        wp_form.addRow("Username:", self.wp_user)
        self.wp_pass = QLineEdit(self.cfg["wordpress"]["app_password"])
        self.wp_pass.setEchoMode(QLineEdit.Password)
        self.wp_pass.setToolTip("Users -> Profile -> Application Passwords -> New (not your login password).")
        wp_form.addRow("Application password:", self.wp_pass)
        layout.addWidget(wp_group)

        test_group = QGroupBox("Test connections")
        test_layout = QVBoxLayout(test_group)
        self.test_conn_btn = QPushButton("Test connections now")
        self.test_conn_btn.clicked.connect(self._start_test_connection)
        test_layout.addWidget(self.test_conn_btn)
        self.fb_status_label = QLabel("Firebird: not tested yet.")
        self.wc_status_label = QLabel("WooCommerce: not tested yet.")
        test_layout.addWidget(self.fb_status_label)
        test_layout.addWidget(self.wc_status_label)
        layout.addWidget(test_group)

        save_btn = QPushButton("Save configuration")
        save_btn.clicked.connect(self._save_config)
        layout.addWidget(save_btn)
        layout.addStretch()
        return widget

    def _start_test_connection(self):
        if self.test_conn_worker and self.test_conn_worker.isRunning():
            return
        cfg = self._collect_config()
        self.test_conn_btn.setEnabled(False)
        self.fb_status_label.setText("Firebird: testing...")
        self.wc_status_label.setText("WooCommerce: testing...")
        self.test_conn_worker = TestConnectionWorker(cfg)
        self.test_conn_worker.finished_ok.connect(self._test_connection_done)
        self.test_conn_worker.finished_error.connect(self._test_connection_failed)
        self.test_conn_worker.start()

    def _test_connection_done(self, result):
        self.test_conn_btn.setEnabled(True)
        fb, wc = result["firebird"], result["woocommerce"]
        self.fb_status_label.setText(f"Firebird: {'OK' if fb['ok'] else 'FAILED'} -- {fb['message']}")
        self.fb_status_label.setStyleSheet(f"color: {'green' if fb['ok'] else 'red'};")
        self.wc_status_label.setText(f"WooCommerce: {'OK' if wc['ok'] else 'FAILED'} -- {wc['message']}")
        self.wc_status_label.setStyleSheet(f"color: {'green' if wc['ok'] else 'red'};")

    def _test_connection_failed(self, message):
        self.test_conn_btn.setEnabled(True)
        self.fb_status_label.setText(f"Firebird: FAILED -- {message}")
        self.fb_status_label.setStyleSheet("color: red;")
        self.wc_status_label.setText("WooCommerce: not tested (unexpected error above).")

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
        self.cfg["sync"]["auto_sale_on_price_drop"] = self.auto_sale_on_price_drop.isChecked()
        self.cfg["woocommerce"]["site_url"] = self.wc_url.text()
        self.cfg["woocommerce"]["consumer_key"] = self.wc_key.text()
        self.cfg["woocommerce"]["consumer_secret"] = self.wc_secret.text()
        self.cfg["wordpress"]["username"] = self.wp_user.text()
        self.cfg["wordpress"]["app_password"] = self.wp_pass.text()
        return self.cfg

    def _save_config(self):
        cfg = self._collect_config()
        save_config(self.config_path, cfg)
        self.statusBar().showMessage(f"Configuration saved to {self.config_path}", 5000)
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

        adopt_group = QGroupBox("First-time setup: adopt existing products")
        adopt_layout = QVBoxLayout(adopt_group)
        adopt_layout.addWidget(QLabel(
            "Run this ONCE if your store already has products (e.g. made by an\n"
            "older tool). It matches existing WooCommerce products to your\n"
            "articles by SKU so stock sync knows their ids. If a plugin hides\n"
            "products without images, disable it (or set it to frontend-only)\n"
            "just while this runs, so every product is visible to the API."
        ))
        self.adopt_btn = QPushButton("Adopt existing products now")
        self.adopt_btn.clicked.connect(self._start_adopt)
        adopt_layout.addWidget(self.adopt_btn)
        layout.addWidget(adopt_group)

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

    def _start_adopt(self):
        if self.adopt_worker and self.adopt_worker.isRunning():
            return
        cfg = self._collect_config()
        self.stock_log_view.clear()
        self.adopt_btn.setEnabled(False)
        self.adopt_worker = AdoptWorker(cfg)
        self.adopt_worker.line.connect(self.stock_log_view.appendPlainText)
        self.adopt_worker.finished_ok.connect(self._adopt_done)
        self.adopt_worker.finished_error.connect(self._adopt_failed)
        self.adopt_worker.start()

    def _adopt_done(self, report):
        self.adopt_btn.setEnabled(True)
        self.stock_log_view.appendPlainText(
            f"Adopted {report['adopted']} of {report['wc_total']} WooCommerce product(s)."
        )

    def _adopt_failed(self, message):
        self.adopt_btn.setEnabled(True)
        self.stock_log_view.appendPlainText(f"FAILED: {message}")

    # -- Orders tab -------------------------------------------------------------
    def _build_orders_tab(self):
        widget = QWidget()
        layout = QVBoxLayout(widget)
        oi_cfg = self.cfg["order_import"]

        note = QLabel(
            "Every run scans the FULL WooCommerce order history for the statuses\n"
            "mapped below (or only orders on/after the date below, if set) --\n"
            "there's no separate \"import past orders\" step. Each order is\n"
            "written to Firebird as PIECE.REFDOC = 'WC-<order id>', and that\n"
            "field is checked before creating anything, so re-running (or\n"
            "scheduling this to run repeatedly) never creates a duplicate\n"
            "document for an order that was already imported."
        )
        note.setWordWrap(True)
        note.setStyleSheet("color: #555;")
        layout.addWidget(note)

        cfg_group = QGroupBox("WooCommerce order -> Firebird document mapping")
        form = QFormLayout(cfg_group)
        self.order_client_code = QLineEdit(oi_cfg["client_code"])
        self.order_client_code.setToolTip("TIERS.CODE_TIERS the generated PIECE is billed to.")
        form.addRow("Client CODE_TIERS:", self.order_client_code)
        self.order_code_depot = QLineEdit(oi_cfg["code_depot"])
        form.addRow("Default CODE_DEPOT:", self.order_code_depot)
        self.order_username = QLineEdit(oi_cfg["username"])
        form.addRow("Username stamped on PIECE:", self.order_username)
        self.order_on_missing_sku = QComboBox()
        self.order_on_missing_sku.addItems(["skip_line", "skip_order"])
        self.order_on_missing_sku.setCurrentText(oi_cfg["on_missing_sku"])
        self.order_on_missing_sku.setToolTip(
            "skip_line: drop just the unmatched line, import the rest of the order.\n"
            "skip_order: if any line's SKU isn't found in ARTICLE, skip the whole order."
        )
        form.addRow("On missing SKU:", self.order_on_missing_sku)
        self.order_cancel_statuses = QLineEdit(", ".join(oi_cfg.get("cancel_statuses") or []))
        self.order_cancel_statuses.setPlaceholderText("e.g. cancelled, refunded")
        self.order_cancel_statuses.setToolTip(
            "WooCommerce statuses that mean 'annul the document(s) already\n"
            "created for this order' instead of creating a new one -- e.g. an\n"
            "order that went processing (document created) then got cancelled.\n"
            "Comma-separated. Leave empty to disable (cancellations are ignored)."
        )
        form.addRow("Cancel statuses:", self.order_cancel_statuses)

        start_date_row = QHBoxLayout()
        self.order_use_start_date = QCheckBox("Only import orders created on/after:")
        self.order_start_date = QDateEdit()
        self.order_start_date.setCalendarPopup(True)
        self.order_start_date.setDisplayFormat("yyyy-MM-dd")
        configured_start = (oi_cfg.get("start_date") or "").strip()
        if configured_start:
            self.order_use_start_date.setChecked(True)
            qd = QDate.fromString(configured_start, "yyyy-MM-dd")
            self.order_start_date.setDate(qd if qd.isValid() else QDate.currentDate())
        else:
            self.order_use_start_date.setChecked(False)
            self.order_start_date.setDate(QDate.currentDate().addMonths(-1))
        self.order_start_date.setEnabled(self.order_use_start_date.isChecked())
        self.order_use_start_date.toggled.connect(self.order_start_date.setEnabled)
        self.order_use_start_date.setToolTip(
            "Skips fetching/importing anything created before this date.\n"
            "Useful to keep a large order history fast to scan each run, or\n"
            "to deliberately leave old orders out. Unchecked = full history."
        )
        start_date_row.addWidget(self.order_use_start_date)
        start_date_row.addWidget(self.order_start_date)
        start_date_row.addStretch()
        form.addRow(start_date_row)
        layout.addWidget(cfg_group)

        types_row = QHBoxLayout()
        self.refresh_types_btn = QPushButton("Load document types from database")
        self.refresh_types_btn.setToolTip(
            "Connects to Firebird and reads LOCAL_TYPE_PIECE so the dropdowns\n"
            "below show real document types (e.g. PC_VE_COM = Commande de vente)\n"
            "instead of requiring you to know the raw codes."
        )
        self.refresh_types_btn.clicked.connect(self._load_type_pieces)
        types_row.addWidget(self.refresh_types_btn)
        types_row.addStretch()
        layout.addLayout(types_row)

        mapping_group = QGroupBox("Status mapping -- which Firebird document each WooCommerce order status creates")
        mapping_layout = QVBoxLayout(mapping_group)
        mapping_layout.addWidget(QLabel(
            "Example: WooCommerce status \"processing\" -> document type "
            "\"PC_VE_COM\" (Commande de vente)."
        ))
        self.status_table = QTableWidget(0, 2)
        self.status_table.setHorizontalHeaderLabels(["WooCommerce order status", "Firebird document type"])
        self.status_table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.status_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        mapping_layout.addWidget(self.status_table)
        status_btn_row = QHBoxLayout()
        add_status_btn = QPushButton("+ Add row")
        add_status_btn.clicked.connect(lambda: self._add_status_mapping_row())
        remove_status_btn = QPushButton("- Remove selected row")
        remove_status_btn.clicked.connect(lambda: self._remove_selected_row(self.status_table))
        status_btn_row.addWidget(add_status_btn)
        status_btn_row.addWidget(remove_status_btn)
        status_btn_row.addStretch()
        mapping_layout.addLayout(status_btn_row)
        layout.addWidget(mapping_group)
        for wc_status, code in oi_cfg["status_mapping"].items():
            self._add_status_mapping_row(wc_status, code)

        transform_group = QGroupBox("Transformation linking -- link a created document back to an earlier one")
        transform_layout = QVBoxLayout(transform_group)
        transform_layout.addWidget(QLabel(
            "Example: a \"PC_VE_B\" (Bon de livraison) created for status "
            "\"completed\" links back to the \"PC_VE_COM\" (Commande de vente)\n"
            "created earlier for the same order, the way turning a commande "
            "into a delivery note would in the ERP."
        ))
        self.transform_table = QTableWidget(0, 2)
        self.transform_table.setHorizontalHeaderLabels(["Newly created document type", "Links back to source document type"])
        self.transform_table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.transform_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        transform_layout.addWidget(self.transform_table)
        transform_btn_row = QHBoxLayout()
        add_transform_btn = QPushButton("+ Add row")
        add_transform_btn.clicked.connect(lambda: self._add_transform_row())
        remove_transform_btn = QPushButton("- Remove selected row")
        remove_transform_btn.clicked.connect(lambda: self._remove_selected_row(self.transform_table))
        transform_btn_row.addWidget(add_transform_btn)
        transform_btn_row.addWidget(remove_transform_btn)
        transform_btn_row.addStretch()
        transform_layout.addLayout(transform_btn_row)
        layout.addWidget(transform_group)
        for target, source in oi_cfg["transformation"].items():
            self._add_transform_row(target, source)

        diag_group = QGroupBox("Diagnostics (results print to the log below)")
        diag_layout = QVBoxLayout(diag_group)
        diag_layout.addWidget(QLabel(
            "These connect to Firebird and print raw results here -- useful if\n"
            "you only have NetFact2 and can't run a SQL query yourself."
        ))
        diag_btn_row = QHBoxLayout()
        self.check_annulee_btn = QPushButton("Check ANNULEE values")
        self.check_annulee_btn.setToolTip(
            "Compares PIECE.ANNULEE on documents you created manually in NetFact2\n"
            "vs. the ones this tool imported (REFDOC starting with 'WC-'). Tells\n"
            "us whether the tool is writing the right 'not cancelled' value."
        )
        self.check_annulee_btn.clicked.connect(self._start_annulee_check)
        self.check_duplicates_btn = QPushButton("Check for duplicate WC imports")
        self.check_duplicates_btn.setToolTip(
            "Finds orders that ended up with more than one PIECE for the same\n"
            "order (evidence of the now-fixed duplicate-reimport bug)."
        )
        self.check_duplicates_btn.clicked.connect(self._start_duplicate_check)
        diag_btn_row.addWidget(self.check_annulee_btn)
        diag_btn_row.addWidget(self.check_duplicates_btn)
        diag_btn_row.addStretch()
        diag_layout.addLayout(diag_btn_row)

        diag_layout.addWidget(QLabel(
            "\nFix duplicates: for any order with more than one active document,\n"
            "keeps the earliest and annuls (ANNULEE=1 on PIECE + ITEM -- the same\n"
            "mechanism the Cancel statuses feature and your own manual NetFact2\n"
            "cleanup use) the rest. This does NOT delete anything from Firebird."
        ))
        fix_btn_row = QHBoxLayout()
        self.preview_fix_btn = QPushButton("Preview duplicate fix (dry run)")
        self.preview_fix_btn.clicked.connect(lambda: self._start_fix_duplicates(dry_run=True))
        self.fix_duplicates_btn = QPushButton("Fix duplicates now")
        self.fix_duplicates_btn.setToolTip(
            "Writes to Firebird. Preview first to see exactly which documents\n"
            "will be annulled before running this for real."
        )
        self.fix_duplicates_btn.clicked.connect(lambda: self._confirm_fix_duplicates())
        fix_btn_row.addWidget(self.preview_fix_btn)
        fix_btn_row.addWidget(self.fix_duplicates_btn)
        fix_btn_row.addStretch()
        diag_layout.addLayout(fix_btn_row)
        layout.addWidget(diag_group)

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

    def _type_piece_combo(self, current_code=""):
        combo = QComboBox()
        combo.setEditable(True)
        for choice in self.type_piece_choices:
            combo.addItem(f"{choice['code']} - {choice['label']}", choice["code"])
        if current_code:
            idx = combo.findData(current_code)
            if idx >= 0:
                combo.setCurrentIndex(idx)
            else:
                combo.setEditText(current_code)
        return combo

    def _add_status_mapping_row(self, wc_status="", code_type_piece=""):
        row = self.status_table.rowCount()
        self.status_table.insertRow(row)
        status_combo = QComboBox()
        status_combo.setEditable(True)
        status_combo.addItems(_COMMON_WC_STATUSES)
        if wc_status:
            status_combo.setCurrentText(wc_status)
        else:
            status_combo.setCurrentText("")
        self.status_table.setCellWidget(row, 0, status_combo)
        self.status_table.setCellWidget(row, 1, self._type_piece_combo(code_type_piece))

    def _add_transform_row(self, target="", source=""):
        row = self.transform_table.rowCount()
        self.transform_table.insertRow(row)
        self.transform_table.setCellWidget(row, 0, self._type_piece_combo(target))
        self.transform_table.setCellWidget(row, 1, self._type_piece_combo(source))

    def _remove_selected_row(self, table):
        rows = sorted({idx.row() for idx in table.selectedIndexes()}, reverse=True)
        for row in rows:
            table.removeRow(row)

    def _load_type_pieces(self):
        if self.type_pieces_worker and self.type_pieces_worker.isRunning():
            return
        cfg = self._collect_config()
        self.refresh_types_btn.setEnabled(False)
        self.refresh_types_btn.setText("Loading...")
        self.type_pieces_worker = TypePiecesWorker(cfg)
        self.type_pieces_worker.finished_ok.connect(self._type_pieces_loaded)
        self.type_pieces_worker.finished_error.connect(self._type_pieces_failed)
        self.type_pieces_worker.start()

    def _type_pieces_loaded(self, choices):
        self.type_piece_choices = choices
        self.refresh_types_btn.setEnabled(True)
        self.refresh_types_btn.setText("Load document types from database")
        for table, columns in ((self.status_table, [1]), (self.transform_table, [0, 1])):
            for row in range(table.rowCount()):
                for col in columns:
                    old_combo = table.cellWidget(row, col)
                    current = _combo_value(old_combo) if old_combo else ""
                    table.setCellWidget(row, col, self._type_piece_combo(current))
        self.statusBar().showMessage(f"Loaded {len(choices)} document type(s) from LOCAL_TYPE_PIECE.", 5000)

    def _type_pieces_failed(self, message):
        self.refresh_types_btn.setEnabled(True)
        self.refresh_types_btn.setText("Load document types from database")
        QMessageBox.warning(self, "Could not load document types", message)

    def _start_annulee_check(self):
        self._start_order_diagnostic("annulee")

    def _start_duplicate_check(self):
        self._start_order_diagnostic("duplicates")

    def _start_order_diagnostic(self, kind):
        if self.order_diag_worker and self.order_diag_worker.isRunning():
            return
        cfg = self._collect_config()
        self.check_annulee_btn.setEnabled(False)
        self.check_duplicates_btn.setEnabled(False)
        self.order_log_view.appendPlainText(f"Running '{kind}' check...")
        self.order_diag_worker = OrderDiagnosticsWorker(cfg, kind)
        self.order_diag_worker.finished_ok.connect(self._order_diagnostic_done)
        self.order_diag_worker.finished_error.connect(self._order_diagnostic_failed)
        self.order_diag_worker.start()

    def _order_diagnostic_done(self, kind, rows):
        self.check_annulee_btn.setEnabled(True)
        self.check_duplicates_btn.setEnabled(True)
        if kind == "annulee":
            self.order_log_view.appendPlainText("--- ANNULEE values: manually-created vs. WC-imported ---")
            if not rows:
                self.order_log_view.appendPlainText("  PIECE has no rows.")
            for source, value, count in rows:
                self.order_log_view.appendPlainText(f"  {source}: ANNULEE={value!r} ({count} document(s))")
        else:
            self.order_log_view.appendPlainText("--- Duplicate WC-imported documents ---")
            if not rows:
                self.order_log_view.appendPlainText("  None found.")
            for refdoc, doc_type, count in rows:
                self.order_log_view.appendPlainText(f"  {refdoc} / {doc_type}: {count} copies")

    def _order_diagnostic_failed(self, kind, message):
        self.check_annulee_btn.setEnabled(True)
        self.check_duplicates_btn.setEnabled(True)
        self.order_log_view.appendPlainText(f"'{kind}' check FAILED: {message}")

    def _confirm_fix_duplicates(self):
        answer = QMessageBox.question(
            self, "Fix duplicate WC imports?",
            "This will annul (ANNULEE=1 on PIECE and ITEM) the extra document(s) "
            "for any order that has more than one active document, keeping only "
            "the earliest. This writes to Firebird.\n\n"
            "Run 'Preview duplicate fix (dry run)' first if you haven't already, "
            "to see exactly what will change.\n\nProceed?",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
        )
        if answer == QMessageBox.Yes:
            self._start_fix_duplicates(dry_run=False)

    def _start_fix_duplicates(self, dry_run):
        if self.fix_dup_worker and self.fix_dup_worker.isRunning():
            return
        cfg = self._collect_config()
        self.preview_fix_btn.setEnabled(False)
        self.fix_duplicates_btn.setEnabled(False)
        self.order_log_view.appendPlainText(
            "Previewing duplicate fix..." if dry_run else "Fixing duplicates..."
        )
        self.fix_dup_worker = FixDuplicatesWorker(cfg, dry_run)
        self.fix_dup_worker.line.connect(self.order_log_view.appendPlainText)
        self.fix_dup_worker.finished_ok.connect(self._fix_duplicates_done)
        self.fix_dup_worker.finished_error.connect(self._fix_duplicates_failed)
        self.fix_dup_worker.start()

    def _fix_duplicates_done(self, report):
        self.preview_fix_btn.setEnabled(True)
        self.fix_duplicates_btn.setEnabled(True)
        self.order_log_view.appendPlainText(
            f"{len(report['fixed'])} order(s) with duplicates, "
            f"{report['total_annulled']} document(s) annulled."
        )

    def _fix_duplicates_failed(self, message):
        self.preview_fix_btn.setEnabled(True)
        self.fix_duplicates_btn.setEnabled(True)
        self.order_log_view.appendPlainText(f"Fix duplicates FAILED: {message}")

    def _collect_order_import_config(self):
        oi_cfg = self.cfg["order_import"]
        oi_cfg["client_code"] = self.order_client_code.text()
        oi_cfg["code_depot"] = self.order_code_depot.text()
        oi_cfg["username"] = self.order_username.text()
        oi_cfg["on_missing_sku"] = self.order_on_missing_sku.currentText()
        oi_cfg["cancel_statuses"] = [
            s.strip() for s in self.order_cancel_statuses.text().split(",") if s.strip()
        ]
        oi_cfg["start_date"] = (
            self.order_start_date.date().toString("yyyy-MM-dd")
            if self.order_use_start_date.isChecked() else ""
        )

        status_mapping = {}
        for row in range(self.status_table.rowCount()):
            status_combo = self.status_table.cellWidget(row, 0)
            code_combo = self.status_table.cellWidget(row, 1)
            wc_status = status_combo.currentText().strip() if status_combo else ""
            code = _combo_value(code_combo) if code_combo else ""
            if wc_status and code:
                status_mapping[wc_status] = code
        oi_cfg["status_mapping"] = status_mapping

        transformation = {}
        for row in range(self.transform_table.rowCount()):
            target_combo = self.transform_table.cellWidget(row, 0)
            source_combo = self.transform_table.cellWidget(row, 1)
            target = _combo_value(target_combo) if target_combo else ""
            source = _combo_value(source_combo) if source_combo else ""
            if target and source:
                transformation[target] = source
        oi_cfg["transformation"] = transformation
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
            f"created={len(report['created'])} cancelled={len(report.get('cancelled', []))} "
            f"skipped={len(report['skipped'])} errors={len(report['errors'])}"
        )
        skip_reasons = report.get("skip_reasons") or {}
        if skip_reasons:
            breakdown = ", ".join(f"{reason}={count}" for reason, count in skip_reasons.items())
            self.order_log_view.appendPlainText(f"Skip reasons: {breakdown}")
            if skip_reasons.get("already_imported"):
                self.order_log_view.appendPlainText(
                    f"  ({skip_reasons['already_imported']} already existed in Firebird -- not duplicated.)"
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
                    windows_task.install(sys.executable, cli_flag, interval, task_name,
                                          self.config_path)
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
