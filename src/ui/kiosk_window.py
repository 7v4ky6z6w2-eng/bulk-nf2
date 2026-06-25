"""Fenêtre principale du kiosque prix (plein écran, sans barre de titre).

Fonctionnement :
  - Écran de repos  : invite bilingue FR/AR, champ de saisie invisible capturant
                      les codes-barres USB (les scanners émettent des touches
                      rapides terminées par Entrée).
  - Après scan      : affichage immédiat du nom + prix ; image chargée en
                      arrière-plan via un QThread dédié pour ne pas bloquer l'UI.
  - Retour au repos : automatique après cfg.ui.idle_reset_seconds secondes, ou
                      dès le prochain scan.
  - Quitter         : raccourci cfg.ui.exit_hotkey (par défaut Ctrl+Alt+Q).
  - Réglages        : raccourci Ctrl+Alt+S pour rouvrir l'assistant.
"""

from __future__ import annotations

import os
import uuid
from typing import Optional

from PySide6.QtCore import (
    QObject,
    QRectF,
    QSize,
    Qt,
    QThread,
    QTimer,
    Signal,
)
from PySide6.QtGui import (
    QColor,
    QFont,
    QFontDatabase,
    QKeySequence,
    QLinearGradient,
    QPainter,
    QPainterPath,
    QPen,
    QPixmap,
    QRadialGradient,
    QShortcut,
    QTransform,
)
try:
    from PySide6.QtSvg import QSvgRenderer
    from PySide6.QtCore import QByteArray as _QByteArray
    _HAS_SVG = True
except ImportError:
    _HAS_SVG = False
from PySide6.QtWidgets import (
    QApplication,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QSizePolicy,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

import i18n
from config import AppConfig
from database import Database, DatabaseError
from woocommerce import WooClient

# ---------------------------------------------------------------------------
# Assets
# ---------------------------------------------------------------------------

_ASSETS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                           "assets")
_PLACEHOLDER_PATH = os.path.join(_ASSETS_DIR, "placeholder.png")

# ---------------------------------------------------------------------------
# Prime Office design tokens (dark theme)
# ---------------------------------------------------------------------------

_C_BG        = "#170D38"   # deep purple background
_C_SURFACE   = "#241551"   # card/surface
_C_SURFACE2  = "#2D1B60"   # surface variant
_C_TEXT      = "#F1EDFB"   # primary text
_C_MUTED     = "#ABA1CB"   # secondary text
_C_LIME      = "#C6F432"   # brand accent — price, highlights
_C_LIME_DARK = "#9CCB14"
_C_VIOLET    = "#5B2EE5"   # secondary accent
_C_LINE      = "#3F2F6C"   # borders
_C_DANGER    = "#D63C5E"   # errors

# Dimensions image produit
_IMAGE_MAX_W = 380
_IMAGE_MAX_H = 380

# Tailles de police (px)
_SZ_BRAND    = 18
_SZ_PROMPT_FR = 34
_SZ_PROMPT_AR = 42
_SZ_DESG     = 38
_SZ_PRICE    = 80
_SZ_CURRENCY = 36
_SZ_ERROR    = 30
_SZ_REF      = 16

# ---------------------------------------------------------------------------
# Prime Office logo SVG (icon paths, ink color — rendered on lime background)
# ---------------------------------------------------------------------------

_LOGO_SVG = b"""<svg xmlns="http://www.w3.org/2000/svg" viewBox="53 16 100 116">
  <g transform="scale(1,-1) translate(0,-148)">
    <path fill="#160B2E" d="M 54.695,16.887 C 54.391,17.047 54.031,17.431 53.863,17.743
      C 53.567,18.295 53.567,20.375 53.567,73.999 C 53.551,125.031 53.583,129.719
      53.831,130.247 C 53.983,130.559 54.327,130.975 54.607,131.175
      C 55.103,131.535 55.287,131.551 59.695,131.607 C 65.303,131.671 65.815,131.623
      66.607,130.879 L 67.223,130.327 L 67.255,74.279
      C 67.287,23.871 67.271,18.175 67.039,17.727 C 66.911,17.447 66.559,17.087
      66.295,16.903 C 65.831,16.607 65.487,16.567 60.519,16.567
      C 55.687,16.567 55.175,16.607 54.695,16.887 Z"/>
    <path fill="#160B2E" fill-rule="evenodd" d="M 107.735,16.903
      C 100.663,17.679 94.751,19.767 89.239,23.439 C 79.983,29.599 73.991,39.335
      72.519,50.575 C 72.191,53.087 72.191,57.711 72.519,60.223
      C 74.007,71.559 80.119,81.399 89.551,87.639 C 95.615,91.639 102.631,93.895
      109.935,94.159 C 122.215,94.623 134.287,89.127 141.831,79.655
      C 144.231,76.647 146.735,72.207 147.959,68.799 C 150.591,61.431 151.023,53.223
      149.151,45.935 C 148.071,41.679 146.271,37.623 143.871,34.055
      C 142.263,31.631 141.503,30.711 139.479,28.607 C 133.655,22.543 125.607,18.455
      116.999,17.167 C 114.847,16.839 109.631,16.703 107.735,16.903 Z
      M 108.591,30.807 C 106.671,31.055 103.975,31.751 102.151,32.479
      C 98.623,33.903 96.063,35.607 93.359,38.335 C 90.015,41.719 87.847,45.807
      86.807,50.751 C 86.295,53.175 86.295,57.311 86.807,59.911
      C 88.591,68.911 94.783,76.063 103.231,78.879 C 106.255,79.887 107.367,80.055
      111.255,80.055 C 114.303,80.055 114.935,80.023 116.439,79.671
      C 127.079,77.287 134.695,68.863 135.991,58.039 C 136.687,52.111 134.943,45.687
      131.303,40.823 C 127.559,35.807 122.167,32.399 116.007,31.151
      C 114.007,30.759 110.367,30.575 108.591,30.807 Z"/>
    <path fill="#160B2E" d="M 146.383,81.759 C 143.567,85.135 139.303,89.191
      136.567,91.063 L 135.727,91.623 L 135.527,94.023
      C 135.231,97.767 134.247,101.527 132.895,104.223
      C 130.807,108.415 126.783,112.183 122.087,114.391
      C 119.207,115.743 115.711,116.719 112.615,117.055
      C 111.591,117.167 104.007,117.215 91.639,117.215 H 72.271 V 123.927 V 130.631
      H 92.751 C 112.103,130.631 113.359,130.615 115.399,130.311
      C 121.751,129.367 128.191,126.983 132.943,123.791
      C 141.599,117.983 146.799,109.783 148.671,99.007
      C 149.463,94.375 149.415,88.199 148.551,83.383
      C 148.143,80.999 147.959,80.303 147.759,80.303
      C 147.663,80.303 147.047,80.967 146.383,81.759 Z"/>
  </g>
</svg>"""


def _make_logo_pixmap(size: int = 80) -> QPixmap:
    """Lime rounded-square with the Prime Office icon — rendered at 4× for sharpness."""
    render = size * 4  # paint at 4× then scale down → crisp on any display
    pm = QPixmap(render, render)
    pm.fill(Qt.transparent)
    painter = QPainter(pm)
    painter.setRenderHint(QPainter.Antialiasing)
    painter.setRenderHint(QPainter.SmoothPixmapTransform)
    radius = render * 0.26
    bg_path = QPainterPath()
    bg_path.addRoundedRect(QRectF(0, 0, render, render), radius, radius)
    painter.fillPath(bg_path, QColor(_C_LIME))
    if _HAS_SVG:
        renderer = QSvgRenderer(_QByteArray(_LOGO_SVG))
        pad = render * 0.12
        renderer.render(painter, QRectF(pad, pad, render - 2 * pad, render - 2 * pad))
    else:
        painter.setPen(QColor("#160B2E"))
        f = QFont(_FONT_HEADING)
        f.setPixelSize(int(render * 0.38))
        f.setWeight(QFont.Bold)
        painter.setFont(f)
        painter.drawText(pm.rect(), Qt.AlignCenter, "PO")
    painter.end()
    return pm.scaled(size, size, Qt.KeepAspectRatio, Qt.SmoothTransformation)


# ---------------------------------------------------------------------------
# Font helpers — tries Unbounded/Rubik (Google Fonts), falls back gracefully
# ---------------------------------------------------------------------------

def _font(family: str, px: int, weight: QFont.Weight = QFont.Normal) -> QFont:
    f = QFont(family)
    f.setPixelSize(px)
    f.setWeight(weight)
    return f


_FONT_HEADING = "Unbounded, Segoe UI Black, Arial Black, sans-serif"
_FONT_BODY    = "Rubik, Segoe UI, Arial, sans-serif"
_FONT_ARABIC  = "Noto Naskh Arabic, Noto Sans Arabic, Arabic Typesetting, Arial"

# ---------------------------------------------------------------------------
# Global stylesheet
# ---------------------------------------------------------------------------

_STYLESHEET = f"""
QMainWindow, QWidget#root {{
    background-color: {_C_BG};
}}
QWidget {{
    background-color: transparent;
    color: {_C_TEXT};
}}
QLabel#brand {{
    color: {_C_LIME};
    font-size: {_SZ_BRAND}px;
    font-weight: 700;
    letter-spacing: 3px;
}}
QLabel#promptFr {{
    color: {_C_TEXT};
    font-size: {_SZ_PROMPT_FR}px;
    font-weight: 500;
}}
QLabel#promptAr {{
    color: {_C_TEXT};
    font-size: {_SZ_PROMPT_AR}px;
}}
QLabel#scanHint {{
    color: {_C_MUTED};
    font-size: 16px;
    font-weight: 400;
    letter-spacing: 1px;
}}
QLabel#designation {{
    color: {_C_TEXT};
    font-size: {_SZ_DESG}px;
    font-weight: 600;
}}
QLabel#price {{
    color: {_C_LIME};
    font-size: {_SZ_PRICE}px;
    font-weight: 700;
}}
QLabel#ref {{
    color: {_C_MUTED};
    font-size: {_SZ_REF}px;
}}
QLabel#image {{
    background-color: transparent;
}}
QLabel#error {{
    color: {_C_DANGER};
    font-size: {_SZ_ERROR}px;
    font-weight: 600;
}}
QLabel#notFound {{
    color: #FF2222;
    font-size: {_SZ_ERROR}px;
    font-weight: 700;
}}
QFrame#divider {{
    background-color: {_C_LINE};
    max-height: 1px;
    min-height: 1px;
}}
QFrame#card {{
    background-color: {_C_SURFACE};
    border-radius: 20px;
    border: 1px solid {_C_LINE};
}}
"""

# ---------------------------------------------------------------------------
# Placeholder pixmap
# ---------------------------------------------------------------------------

def _make_placeholder_pixmap(w: int = _IMAGE_MAX_W, h: int = _IMAGE_MAX_H) -> QPixmap:
    pm = QPixmap(w, h)
    pm.fill(Qt.transparent)
    painter = QPainter(pm)
    painter.setRenderHint(QPainter.Antialiasing)
    # rounded rect fill
    path = QPainterPath()
    path.addRoundedRect(QRectF(0, 0, w, h), 16, 16)
    painter.fillPath(path, QColor(_C_SURFACE2))
    # dashed border
    pen = QPen(QColor(_C_LINE))
    pen.setWidth(2)
    pen.setStyle(Qt.DashLine)
    painter.setPen(pen)
    painter.drawPath(path)
    # label
    painter.setPen(QColor(_C_MUTED))
    f = QFont(_FONT_BODY)
    f.setPixelSize(18)
    painter.setFont(f)
    painter.drawText(pm.rect(), Qt.AlignCenter, "Image\nnon disponible")
    painter.end()
    return pm


def _load_placeholder() -> QPixmap:
    if os.path.exists(_PLACEHOLDER_PATH):
        pm = QPixmap(_PLACEHOLDER_PATH)
        if not pm.isNull():
            return pm.scaled(_IMAGE_MAX_W, _IMAGE_MAX_H,
                             Qt.KeepAspectRatio, Qt.SmoothTransformation)
    return _make_placeholder_pixmap()


# ---------------------------------------------------------------------------
# Worker — DB lookup (async so UI never freezes between scans)
# ---------------------------------------------------------------------------

class _LookupWorker(QObject):
    # emits (scan_id, article_or_None, db_error_or_None)
    finished = Signal(str, object, object)

    def __init__(self, cfg, code: str, scan_id: str):
        super().__init__()
        self._cfg = cfg   # FirebirdConfig — create a fresh connection per thread
        self._code = code
        self._scan_id = scan_id

    def run(self) -> None:
        from database import Database
        db = Database(self._cfg)
        try:
            article = db.lookup_article(self._code)
            self.finished.emit(self._scan_id, article, None)
        except Exception as exc:
            self.finished.emit(self._scan_id, None, exc)
        finally:
            db.close()


# ---------------------------------------------------------------------------
# Worker — image loading
# ---------------------------------------------------------------------------

class _ImageWorker(QObject):
    finished = Signal(str, object)

    def __init__(self, woo: WooClient, ref_art: str, scan_id: str):
        super().__init__()
        self._woo = woo
        self._ref_art = ref_art
        self._scan_id = scan_id

    def run(self) -> None:
        path = self._woo.get_image(self._ref_art)
        self.finished.emit(self._scan_id, path)


# ---------------------------------------------------------------------------
# Brand header bar (common to both screens)
# ---------------------------------------------------------------------------

class _BrandBar(QWidget):
    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setFixedHeight(130)

        # Outer HBox centres the logo column
        outer = QHBoxLayout(self)
        outer.setContentsMargins(0, 12, 0, 12)

        outer.addStretch()

        col = QWidget()
        vl = QVBoxLayout(col)
        vl.setContentsMargins(0, 0, 0, 0)
        vl.setSpacing(6)
        vl.setAlignment(Qt.AlignHCenter)

        # Logo mark — big and sharp
        logo_lbl = QLabel()
        logo_lbl.setAlignment(Qt.AlignCenter)
        logo_lbl.setFixedSize(80, 80)
        logo_lbl.setPixmap(_make_logo_pixmap(80))
        vl.addWidget(logo_lbl, alignment=Qt.AlignHCenter)

        # "PRIME OFFICE" under the logo
        lbl = QLabel("PRIME OFFICE")
        lbl.setObjectName("brand")
        f = QFont(_FONT_HEADING)
        f.setPixelSize(22)
        f.setWeight(QFont.Bold)
        lbl.setFont(f)
        lbl.setAlignment(Qt.AlignCenter)
        vl.addWidget(lbl, alignment=Qt.AlignHCenter)

        outer.addWidget(col)
        outer.addStretch()

    def paintEvent(self, event):  # type: ignore[override]
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.fillRect(self.rect(), QColor(_C_SURFACE))
        # bottom border
        pen = QPen(QColor(_C_LINE))
        pen.setWidth(1)
        painter.setPen(pen)
        painter.drawLine(0, self.height() - 1, self.width(), self.height() - 1)
        painter.end()
        super().paintEvent(event)


# ---------------------------------------------------------------------------
# Idle screen
# ---------------------------------------------------------------------------

class _IdleScreen(QWidget):
    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        outer.addWidget(_BrandBar())

        # center content
        center = QWidget()
        layout = QVBoxLayout(center)
        layout.setAlignment(Qt.AlignCenter)
        layout.setSpacing(24)
        layout.setContentsMargins(60, 60, 60, 60)

        # scan icon (lime circle with barcode lines)
        icon_lbl = QLabel()
        icon_lbl.setAlignment(Qt.AlignCenter)
        icon_lbl.setFixedSize(100, 100)
        icon_pm = self._make_scan_icon(100)
        icon_lbl.setPixmap(icon_pm)
        layout.addWidget(icon_lbl, alignment=Qt.AlignCenter)

        layout.addSpacing(10)

        # French prompt
        lbl_fr = QLabel(i18n.SCAN_PROMPT_FR)
        lbl_fr.setObjectName("promptFr")
        lbl_fr.setAlignment(Qt.AlignCenter)
        lbl_fr.setWordWrap(True)
        f_fr = QFont(_FONT_BODY)
        f_fr.setPixelSize(_SZ_PROMPT_FR)
        f_fr.setWeight(QFont.Medium)
        lbl_fr.setFont(f_fr)
        layout.addWidget(lbl_fr)

        # Arabic prompt
        lbl_ar = QLabel(i18n.SCAN_PROMPT_AR)
        lbl_ar.setObjectName("promptAr")
        lbl_ar.setAlignment(Qt.AlignCenter)
        lbl_ar.setLayoutDirection(Qt.RightToLeft)
        lbl_ar.setWordWrap(True)
        f_ar = QFont(_FONT_ARABIC)
        f_ar.setPixelSize(_SZ_PROMPT_AR)
        lbl_ar.setFont(f_ar)
        layout.addWidget(lbl_ar)

        layout.addSpacing(16)

        # Hint
        hint = QLabel("↵  Appuyez sur Entrée après le code")
        hint.setObjectName("scanHint")
        hint.setAlignment(Qt.AlignCenter)
        f_hint = QFont(_FONT_BODY)
        f_hint.setPixelSize(16)
        hint.setFont(f_hint)
        layout.addWidget(hint)

        outer.addWidget(center, stretch=1)

    @staticmethod
    def _make_scan_icon(size: int) -> QPixmap:
        pm = QPixmap(size, size)
        pm.fill(Qt.transparent)
        p = QPainter(pm)
        p.setRenderHint(QPainter.Antialiasing)
        # lime circle
        grad = QRadialGradient(size / 2, size / 2, size / 2)
        grad.setColorAt(0, QColor(_C_LIME + "40"))
        grad.setColorAt(1, QColor(_C_LIME + "10"))
        path = QPainterPath()
        path.addEllipse(QRectF(0, 0, size, size))
        p.fillPath(path, grad)
        # barcode lines
        pen = QPen(QColor(_C_LIME))
        pen.setCapStyle(Qt.RoundCap)
        cx, cy = size // 2, size // 2
        bar_h = size * 0.38
        widths = [2, 4, 2, 6, 2, 4, 2]
        gaps   = [4, 3, 5, 3, 4, 3, 0]
        total_w = sum(widths) + sum(gaps)
        x = cx - total_w // 2
        for w, g in zip(widths, gaps):
            pen.setWidth(w)
            p.setPen(pen)
            p.drawLine(int(x + w / 2), int(cy - bar_h / 2),
                       int(x + w / 2), int(cy + bar_h / 2))
            x += w + g
        p.end()
        return pm

    def paintEvent(self, event):  # type: ignore[override]
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor(_C_BG))
        # subtle radial glow top-right
        grad = QRadialGradient(self.width(), 0, self.width() * 0.8)
        grad.setColorAt(0, QColor(_C_VIOLET + "40"))
        grad.setColorAt(1, QColor(_C_BG + "00"))
        painter.fillRect(self.rect(), grad)
        painter.end()
        super().paintEvent(event)


# ---------------------------------------------------------------------------
# Result screen
# ---------------------------------------------------------------------------

class _ResultScreen(QWidget):
    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        outer.addWidget(_BrandBar())

        # scrollable center
        center = QWidget()
        layout = QVBoxLayout(center)
        layout.setAlignment(Qt.AlignCenter)
        layout.setSpacing(0)
        layout.setContentsMargins(60, 40, 60, 40)

        # --- card
        self._card = QFrame()
        self._card.setObjectName("card")
        card_layout = QVBoxLayout(self._card)
        card_layout.setSpacing(16)
        card_layout.setContentsMargins(32, 32, 32, 32)

        # image
        self._image_label = QLabel()
        self._image_label.setObjectName("image")
        self._image_label.setAlignment(Qt.AlignCenter)
        self._image_label.setFixedSize(_IMAGE_MAX_W, _IMAGE_MAX_H)
        card_layout.addWidget(self._image_label, alignment=Qt.AlignCenter)

        # divider
        div = QFrame()
        div.setObjectName("divider")
        div.setFrameShape(QFrame.HLine)
        card_layout.addWidget(div)

        # designation
        self._designation_label = QLabel()
        self._designation_label.setObjectName("designation")
        self._designation_label.setAlignment(Qt.AlignCenter)
        self._designation_label.setWordWrap(True)
        f_desg = QFont(_FONT_BODY)
        f_desg.setPixelSize(_SZ_DESG)
        f_desg.setWeight(QFont.DemiBold)
        self._designation_label.setFont(f_desg)
        card_layout.addWidget(self._designation_label)

        # price row
        price_row = QWidget()
        price_hl = QHBoxLayout(price_row)
        price_hl.setAlignment(Qt.AlignCenter)
        price_hl.setSpacing(4)

        self._price_label = QLabel()
        self._price_label.setObjectName("price")
        self._price_label.setAlignment(Qt.AlignCenter)
        f_price = QFont(_FONT_HEADING)
        f_price.setPixelSize(_SZ_PRICE)
        f_price.setWeight(QFont.Bold)
        self._price_label.setFont(f_price)
        price_hl.addWidget(self._price_label)
        card_layout.addWidget(price_row)

        # ref
        self._ref_label = QLabel()
        self._ref_label.setObjectName("ref")
        self._ref_label.setAlignment(Qt.AlignCenter)
        f_ref = QFont(_FONT_BODY)
        f_ref.setPixelSize(_SZ_REF)
        self._ref_label.setFont(f_ref)
        card_layout.addWidget(self._ref_label)

        # errors
        self._error_fr = QLabel()
        self._error_fr.setObjectName("error")
        self._error_fr.setAlignment(Qt.AlignCenter)
        self._error_fr.setWordWrap(True)
        f_err = QFont(_FONT_BODY)
        f_err.setPixelSize(_SZ_ERROR)
        self._error_fr.setFont(f_err)

        self._error_ar = QLabel()
        self._error_ar.setObjectName("error")
        self._error_ar.setAlignment(Qt.AlignCenter)
        self._error_ar.setLayoutDirection(Qt.RightToLeft)
        self._error_ar.setWordWrap(True)
        f_ar = QFont(_FONT_ARABIC)
        f_ar.setPixelSize(_SZ_ERROR)
        self._error_ar.setFont(f_ar)

        card_layout.addWidget(self._error_fr)
        card_layout.addWidget(self._error_ar)

        layout.addWidget(self._card)
        outer.addWidget(center, stretch=1)

        self._placeholder = _load_placeholder()

    def paintEvent(self, event):  # type: ignore[override]
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor(_C_BG))
        grad = QRadialGradient(0, self.height(), self.width() * 0.9)
        grad.setColorAt(0, QColor(_C_VIOLET + "33"))
        grad.setColorAt(1, QColor(_C_BG + "00"))
        painter.fillRect(self.rect(), grad)
        painter.end()
        super().paintEvent(event)

    def show_article(self, designation: str, prix_str: str, ref_art: str = "") -> None:
        self._image_label.setPixmap(self._placeholder)
        self._designation_label.setText(designation)
        self._price_label.setText(prix_str)
        self._ref_label.setText(f"Réf : {ref_art}" if ref_art else "")
        self._designation_label.show()
        self._price_label.show()
        self._ref_label.show()
        self._error_fr.hide()
        self._error_ar.hide()
        self._card.show()

    def set_image(self, path: Optional[str]) -> None:
        if path and os.path.exists(path):
            pm = QPixmap(path)
            if not pm.isNull():
                pm = pm.scaled(_IMAGE_MAX_W, _IMAGE_MAX_H,
                               Qt.KeepAspectRatio, Qt.SmoothTransformation)
                self._image_label.setPixmap(pm)
                return
        self._image_label.setPixmap(self._placeholder)

    def show_not_found(self) -> None:
        self._image_label.setPixmap(self._placeholder)
        self._designation_label.hide()
        self._price_label.hide()
        self._ref_label.hide()
        self._error_fr.setObjectName("notFound")
        self._error_ar.setObjectName("notFound")
        self._error_fr.setText(i18n.NOT_FOUND_FR)
        self._error_ar.setText(i18n.NOT_FOUND_AR)
        self._error_fr.show()
        self._error_ar.show()
        self._card.show()

    def show_db_error(self) -> None:
        self._image_label.setPixmap(self._placeholder)
        self._designation_label.hide()
        self._price_label.hide()
        self._ref_label.hide()
        self._error_fr.setObjectName("error")
        self._error_ar.setObjectName("error")
        self._error_fr.setText(i18n.DB_ERROR_FR)
        self._error_ar.setText(i18n.DB_ERROR_AR)
        self._error_fr.show()
        self._error_ar.show()
        self._card.show()


# ---------------------------------------------------------------------------
# Fenêtre principale kiosque
# ---------------------------------------------------------------------------

class KioskWindow(QMainWindow):
    """Fenêtre kiosque plein-écran sans décoration."""

    def __init__(self, cfg: AppConfig, db: Database, woo: WooClient,
                 parent: Optional[QWidget] = None):
        super().__init__(parent)
        self._cfg = cfg
        self._db = db
        self._woo = woo

        # Identifiant du scan en cours (pour ignorer les résultats en retard)
        self._current_scan_id: Optional[str] = None

        # Threads secondaires
        self._lookup_thread: Optional[QThread] = None
        self._img_thread: Optional[QThread] = None

        # --- Fenêtre sans décoration
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.Window)
        self.setObjectName("centralWidget")
        self.setStyleSheet(_STYLESHEET)

        # --- Widget central
        central = QWidget()
        central.setObjectName("centralWidget")
        self.setCentralWidget(central)
        outer = QVBoxLayout(central)
        outer.setContentsMargins(0, 0, 0, 0)

        # --- QStackedWidget : page 0 = repos, page 1 = résultat
        self._stack = QStackedWidget()
        self._idle_screen = _IdleScreen()
        self._result_screen = _ResultScreen()
        self._stack.addWidget(self._idle_screen)   # index 0
        self._stack.addWidget(self._result_screen)  # index 1
        outer.addWidget(self._stack)

        # --- Champ de saisie invisible pour la capture code-barres
        self._barcode_input = QLineEdit(central)
        self._barcode_input.setFixedSize(1, 1)
        self._barcode_input.move(-10, -10)
        # Totalement transparent pour ne pas perturber l'affichage
        self._barcode_input.setStyleSheet(
            "background: transparent; border: none; color: transparent;"
        )
        self._barcode_input.returnPressed.connect(self._on_barcode)
        self._barcode_input.installEventFilter(self)

        # --- Minuterie de retour à l'écran de repos
        self._idle_timer = QTimer(self)
        self._idle_timer.setSingleShot(True)
        self._idle_timer.timeout.connect(self._show_idle)

        # --- Raccourcis clavier
        QShortcut(QKeySequence(cfg.ui.exit_hotkey), self).activated.connect(
            self.close
        )
        QShortcut(QKeySequence("Ctrl+Alt+S"), self).activated.connect(
            self._open_setup
        )

        # Démarrage sur l'écran de repos
        self._show_idle()

    # --- Affichage plein écran (si activé dans la config) ------------------

    def showEvent(self, event):  # type: ignore[override]
        super().showEvent(event)
        if self._cfg.ui.fullscreen:
            self.showFullScreen()
        self._focus_barcode()

    # --- Capture du focus --------------------------------------------------

    def _focus_barcode(self) -> None:
        """Donne le focus au champ de saisie (le scanner tape toujours dedans)."""
        self._barcode_input.setFocus(Qt.OtherFocusReason)

    def eventFilter(self, obj, event):  # type: ignore[override]
        """Re-capture le focus si le champ le perd."""
        from PySide6.QtCore import QEvent
        if obj is self._barcode_input and event.type() == QEvent.FocusOut:
            QTimer.singleShot(50, self._focus_barcode)
        return super().eventFilter(obj, event)

    def mousePressEvent(self, event):  # type: ignore[override]
        """Clic sur la fenêtre → remettre le focus sur le champ de saisie."""
        self._focus_barcode()
        super().mousePressEvent(event)

    # --- Transitions d'écran -----------------------------------------------

    def _show_idle(self) -> None:
        self._idle_timer.stop()
        self._current_scan_id = None
        self._stack.setCurrentIndex(0)
        self._focus_barcode()

    def _show_result(self) -> None:
        self._stack.setCurrentIndex(1)
        # Déclenche le retour automatique à l'écran de repos
        self._idle_timer.start(self._cfg.ui.idle_reset_seconds * 1000)
        self._focus_barcode()

    # --- Traitement d'un scan ----------------------------------------------

    def _on_barcode(self) -> None:
        code = self._barcode_input.text().strip()
        self._barcode_input.clear()
        if not code:
            return

        self._idle_timer.stop()

        # Abandon de l'éventuel lookup précédent
        if self._lookup_thread is not None and self._lookup_thread.isRunning():
            self._lookup_thread.quit()

        scan_id = str(uuid.uuid4())
        self._current_scan_id = scan_id
        # Go back to idle screen WITHOUT calling _show_idle() — that would reset scan_id to None
        self._stack.setCurrentIndex(0)
        self._focus_barcode()

        thread = QThread(self)
        worker = _LookupWorker(self._cfg.firebird, code, scan_id)
        worker.moveToThread(thread)
        worker.finished.connect(self._on_lookup_done)
        thread.started.connect(worker.run)
        thread.finished.connect(thread.deleteLater)
        self._lookup_thread = thread
        self._lookup_worker = worker  # type: ignore[attr-defined]
        thread.start()

    def _on_lookup_done(self, scan_id: str, article: object, error: object) -> None:
        """Appelé dans le thread principal quand la requête DB répond."""
        if scan_id != self._current_scan_id:
            return  # scan annulé par un nouveau code

        if error is not None:
            self._result_screen.show_db_error()
            self._show_result()
            return

        if article is None:
            self._result_screen.show_not_found()
            self._show_result()
            return

        from database import Article as _Article
        art: _Article = article  # type: ignore[assignment]
        self._result_screen.show_article(
            art.designation,
            i18n.format_price(art.prix_vente_ht),
            art.ref_art,
        )
        self._show_result()
        self._start_image_fetch(art.ref_art, scan_id)

    # --- Chargement asynchrone de l'image ----------------------------------

    def _start_image_fetch(self, ref_art: str, scan_id: str) -> None:
        """Lance le téléchargement de l'image dans un thread secondaire."""
        # Abandon de l'éventuel thread précédent (il se terminera naturellement)
        if self._img_thread is not None and self._img_thread.isRunning():
            self._img_thread.quit()
            # Ne pas attendre (on ne bloque jamais l'UI)

        thread = QThread(self)
        worker = _ImageWorker(self._woo, ref_art, scan_id)
        worker.moveToThread(thread)
        worker.finished.connect(self._on_image_ready)
        thread.started.connect(worker.run)
        thread.finished.connect(thread.deleteLater)
        # Conserver une référence pour éviter la collecte garbage prématurée
        self._img_thread = thread
        self._img_worker = worker  # type: ignore[attr-defined]
        thread.start()

    def _on_image_ready(self, scan_id: str, path: Optional[str]) -> None:
        """Appelé dans le thread principal via signal/slot Qt."""
        # Ignorer si l'écran a déjà changé (nouveau scan ou retour au repos)
        if scan_id != self._current_scan_id:
            return
        self._result_screen.set_image(path)

    # --- Réouverture de l'assistant de configuration ----------------------

    def _open_setup(self) -> None:
        from ui.setup_window import SetupDialog
        from config import load, save
        cfg = load()
        dlg = SetupDialog(cfg, parent=self)
        if dlg.exec():
            # Recharger la config après sauvegarde
            self._cfg = load()
