"""Visual layer for the GUI: one stylesheet, plus a few small reusable widgets.

Kept separate from main_window.py so the look can change without touching
any of the sync/diagnostic logic wired up there.
"""

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QFrame, QHBoxLayout, QLabel, QPushButton, QScrollArea, QSizePolicy,
    QToolButton, QVBoxLayout, QWidget,
)

ACCENT = "#4f46e5"
ACCENT_HOVER = "#4338ca"
DANGER = "#dc2626"
DANGER_HOVER = "#b91c1c"
OK = "#16a34a"
WARN = "#d97706"

STYLESHEET = f"""
* {{ font-family: "Segoe UI", "Inter", "Helvetica Neue", Arial, sans-serif; font-size: 13px; }}
QMainWindow, QScrollArea, QStackedWidget > QWidget {{ background: #f6f7fb; }}
QScrollArea {{ border: none; }}
QWidget#page {{ background: #f6f7fb; }}

/* ---- sidebar ---- */
QFrame#sidebar {{ background: #111827; }}
QLabel#brand {{ color: #ffffff; font-size: 16px; font-weight: 700; padding: 18px 18px 2px 18px; }}
QLabel#brandSub {{ color: #9ca3af; font-size: 11px; padding: 0 18px 14px 18px; }}
QLabel#navSection {{ color: #6b7280; font-size: 10px; font-weight: 700; padding: 14px 18px 4px 18px; }}
QPushButton#nav {{
    color: #d1d5db; background: transparent; border: none; text-align: left;
    padding: 10px 18px; margin: 1px 10px; border-radius: 8px; font-size: 13px;
}}
QPushButton#nav:hover {{ background: #1f2937; color: #ffffff; }}
QPushButton#nav:checked {{ background: {ACCENT}; color: #ffffff; font-weight: 600; }}

/* ---- page header ---- */
QLabel#pageTitle {{ font-size: 22px; font-weight: 700; color: #111827; }}
QLabel#pageSubtitle {{ font-size: 13px; color: #6b7280; }}

/* ---- cards (QGroupBox) ---- */
QGroupBox {{
    background: #ffffff; border: 1px solid #e5e7eb; border-radius: 12px;
    margin-top: 22px; padding: 18px 16px 14px 16px; font-weight: 600; color: #111827;
}}
QGroupBox::title {{ subcontrol-origin: margin; subcontrol-position: top left; left: 14px; padding: 0 6px; background: #f6f7fb; }}
QFrame#card {{ background: #ffffff; border: 1px solid #e5e7eb; border-radius: 12px; }}
QFrame#card:hover {{ border-color: {ACCENT}; }}
QLabel#cardTitle {{ font-size: 15px; font-weight: 700; color: #111827; }}
QLabel#cardText {{ color: #6b7280; }}
QLabel#hint {{ color: #6b7280; }}
QLabel#banner {{ background: #eef2ff; color: #3730a3; border: 1px solid #c7d2fe; border-radius: 10px; padding: 12px 14px; }}

/* ---- inputs ---- */
QLineEdit, QSpinBox, QComboBox, QDateEdit {{
    background: #ffffff; border: 1px solid #d1d5db; border-radius: 8px; padding: 7px 10px; min-height: 18px;
}}
QLineEdit:focus, QSpinBox:focus, QComboBox:focus, QDateEdit:focus {{ border: 1px solid {ACCENT}; }}
QCheckBox {{ spacing: 8px; }}
QPlainTextEdit {{
    background: #0f172a; color: #e2e8f0; border-radius: 10px; border: none; padding: 10px;
    font-family: "Cascadia Mono", "Consolas", "Menlo", monospace; font-size: 12px;
}}
QTableWidget {{ background: #ffffff; border: 1px solid #e5e7eb; border-radius: 10px; gridline-color: #f1f5f9; }}
QHeaderView::section {{ background: #f9fafb; border: none; border-bottom: 1px solid #e5e7eb; padding: 8px; font-weight: 600; color: #374151; }}

/* ---- buttons ---- */
QPushButton {{
    background: #ffffff; color: #111827; border: 1px solid #d1d5db; border-radius: 8px; padding: 8px 16px;
}}
QPushButton:hover {{ background: #f3f4f6; }}
QPushButton:disabled {{ color: #9ca3af; background: #f3f4f6; }}
QPushButton[role="primary"] {{ background: {ACCENT}; color: #ffffff; border: none; font-weight: 600; }}
QPushButton[role="primary"]:hover {{ background: {ACCENT_HOVER}; }}
QPushButton[role="primary"]:disabled {{ background: #c7d2fe; color: #ffffff; }}
QPushButton[role="danger"] {{ background: {DANGER}; color: #ffffff; border: none; font-weight: 600; }}
QPushButton[role="danger"]:hover {{ background: {DANGER_HOVER}; }}
QPushButton[role="danger"]:disabled {{ background: #fecaca; color: #ffffff; }}
QPushButton#collapse {{ background: transparent; border: none; font-weight: 600; font-size: 13px; color: #374151; padding: 8px 2px; text-align: left; }}
QPushButton#collapse:hover {{ background: transparent; color: {ACCENT}; }}

QStatusBar {{ background: #ffffff; border-top: 1px solid #e5e7eb; color: #4b5563; }}
QProgressBar {{ border: none; background: #e5e7eb; border-radius: 4px; max-height: 8px; }}
QProgressBar::chunk {{ background: {ACCENT}; border-radius: 4px; }}
"""

# Button text -> visual role. Checked in order; first match wins.
_DANGER_WORDS = ("fix duplicates now",)
_PRIMARY_WORDS = (
    " now", "apply", "create ", "run ", "find ", "save configuration",
    "test connections", "install", "adopt",
)
_SECONDARY_WORDS = ("preview", "dry run", "list ", "load ", "browse", "check ", "look up", "+ add", "- remove")


def role_for(text):
    t = text.lower()
    if any(w in t for w in _DANGER_WORDS):
        return "danger"
    if any(w in t for w in _SECONDARY_WORDS):
        return None
    if any(w in t for w in _PRIMARY_WORDS):
        return "primary"
    return None


def style_buttons(root):
    """Marks every QPushButton under `root` with a role based on its label so
    the stylesheet can make the real actions stand out from previews."""
    for btn in root.findChildren(QPushButton):
        if btn.objectName() in ("nav", "collapse"):
            continue
        role = role_for(btn.text())
        btn.setProperty("role", role or "")
        btn.setCursor(Qt.PointingHandCursor)
        btn.style().unpolish(btn)
        btn.style().polish(btn)


def tidy_labels(root):
    """Older screens wrapped their help text with hard line breaks, which
    leaves it in a narrow column on a wide window. Reflow it instead."""
    for lbl in root.findChildren(QLabel):
        if lbl.objectName() or lbl.textFormat() == Qt.RichText:
            continue
        text = lbl.text()
        if "\n" in text:
            lbl.setText(text.replace("\n\n", "\u0000").replace("\n", " ").replace("\u0000", "\n\n"))
            lbl.setWordWrap(True)
            lbl.setObjectName("hint")


def page_header(title, subtitle=""):
    box = QWidget()
    lay = QVBoxLayout(box)
    lay.setContentsMargins(0, 0, 0, 6)
    lay.setSpacing(2)
    t = QLabel(title)
    t.setObjectName("pageTitle")
    lay.addWidget(t)
    if subtitle:
        s = QLabel(subtitle)
        s.setObjectName("pageSubtitle")
        s.setWordWrap(True)
        lay.addWidget(s)
    return box


def hint(text):
    lbl = QLabel(text)
    lbl.setObjectName("hint")
    lbl.setWordWrap(True)
    return lbl


def banner(text):
    lbl = QLabel(text)
    lbl.setObjectName("banner")
    lbl.setWordWrap(True)
    return lbl


def scroll_page(inner):
    """Wraps a page in a scroll area with comfortable margins."""
    inner.setObjectName("page")
    if inner.layout() is not None:
        inner.layout().setContentsMargins(28, 24, 28, 24)
        inner.layout().setSpacing(14)
    area = QScrollArea()
    area.setWidgetResizable(True)
    area.setFrameShape(QFrame.NoFrame)
    area.setWidget(inner)
    return area


class CollapsibleSection(QWidget):
    """A titled section that starts collapsed -- keeps rarely-needed
    advanced controls out of a first-time user's way without removing them."""

    def __init__(self, title, content, expanded=False):
        super().__init__()
        self._content = content
        self._title = title
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(4)
        self._toggle = QPushButton()
        self._toggle.setObjectName("collapse")
        self._toggle.setCheckable(True)
        self._toggle.setChecked(expanded)
        self._toggle.setCursor(Qt.PointingHandCursor)
        self._toggle.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self._toggle.toggled.connect(self._apply)
        lay.addWidget(self._toggle)
        lay.addWidget(content)
        self._apply(expanded)

    def _apply(self, expanded):
        self._toggle.setText(("▾  " if expanded else "▸  ") + self._title)
        self._content.setVisible(expanded)


def action_card(title, text, button_text, on_click):
    """Big clickable summary card used on the Home page."""
    card = QFrame()
    card.setObjectName("card")
    lay = QVBoxLayout(card)
    lay.setContentsMargins(18, 16, 18, 16)
    lay.setSpacing(6)
    t = QLabel(title)
    t.setObjectName("cardTitle")
    lay.addWidget(t)
    d = QLabel(text)
    d.setObjectName("cardText")
    d.setWordWrap(True)
    lay.addWidget(d)
    lay.addStretch()
    row = QHBoxLayout()
    btn = QPushButton(button_text)
    btn.clicked.connect(on_click)
    row.addWidget(btn)
    row.addStretch()
    lay.addLayout(row)
    return card
