"""Fenêtre principale du kiosque prix (plein écran, sans barre de titre).

Fonctionnement :
  - Écran de repos  : invite bilingue FR/AR, icône code-barres animée.
  - Après scan      : affichage plein-écran du nom + PRIX géant animé.
  - Retour au repos : automatique après cfg.ui.idle_reset_seconds secondes.
  - Quitter         : raccourci cfg.ui.exit_hotkey (par défaut Ctrl+Alt+Q).
  - Réglages        : raccourci Ctrl+Alt+S pour rouvrir l'assistant.
"""

from __future__ import annotations

import math
import os
import uuid
from typing import Optional

from PySide6.QtCore import (
    QEasingCurve,
    QObject,
    QPropertyAnimation,
    QRectF,
    Qt,
    QThread,
    QTimer,
    Signal,
)
from PySide6.QtGui import (
    QBrush,
    QColor,
    QFont,
    QKeySequence,
    QLinearGradient,
    QPainter,
    QPainterPath,
    QPen,
    QPixmap,
    QRadialGradient,
    QShortcut,
)
try:
    from PySide6.QtSvg import QSvgRenderer
    from PySide6.QtCore import QByteArray as _QByteArray
    _HAS_SVG = True
except ImportError:
    _HAS_SVG = False
from PySide6.QtWidgets import (
    QFrame,
    QGraphicsDropShadowEffect,
    QGraphicsOpacityEffect,
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
# Design tokens — Prime Office dark theme
# ---------------------------------------------------------------------------

_C_BG         = "#0A0618"   # near-black base
_C_BG2        = "#170D38"   # brand bg
_C_SURFACE    = "#1A0F40"   # card surface
_C_SURFACE2   = "#241655"   # surface variant
_C_TEXT       = "#F1EDFB"   # primary text
_C_MUTED      = "#8A80B4"   # muted text
_C_LIME       = "#C6F432"   # accent — price
_C_LIME_DARK  = "#9CCB14"   # darker lime
_C_LIME_GLOW  = "#D4FF3A"   # brightest lime for glow
_C_VIOLET     = "#5B2EE5"   # violet accent
_C_PURPLE     = "#7A4FFF"   # bright purple
_C_LINE       = "#261850"   # borders
_C_DANGER     = "#FF3355"   # not-found / error red

# Fonts
_FONT_HEADING = "Unbounded, Segoe UI Black, Arial Black, sans-serif"
_FONT_BODY    = "Rubik, Segoe UI, Arial, sans-serif"
_FONT_ARABIC  = "Noto Naskh Arabic, Noto Sans Arabic, Arabic Typesetting, Arial"

# Font sizes (px)
_SZ_BRAND     = 15
_SZ_PROMPT_FR = 42
_SZ_PROMPT_AR = 50
_SZ_DESG      = 38
_SZ_PRICE_NUM = 160   # the big hero number
_SZ_PRICE_DA  = 52    # "DA" currency tag
_SZ_ERROR     = 44
_SZ_REF       = 14

# ---------------------------------------------------------------------------
# Prime Office logo SVG
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


def _make_logo_pixmap(size: int = 44) -> QPixmap:
    rs = size * 6
    pm = QPixmap(rs, rs)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    p.setRenderHint(QPainter.SmoothPixmapTransform)
    radius = rs * 0.22
    path = QPainterPath()
    path.addRoundedRect(QRectF(0, 0, rs, rs), radius, radius)
    bg = QLinearGradient(0, 0, 0, rs)
    bg.setColorAt(0, QColor(_C_LIME))
    bg.setColorAt(1, QColor(_C_LIME_DARK))
    p.fillPath(path, QBrush(bg))
    if _HAS_SVG:
        rdr = QSvgRenderer(_QByteArray(_LOGO_SVG))
        pad = rs * 0.11
        rdr.render(p, QRectF(pad, pad, rs - 2 * pad, rs - 2 * pad))
    else:
        p.setPen(QColor("#160B2E"))
        f = QFont(_FONT_HEADING)
        f.setPixelSize(int(rs * 0.36))
        f.setWeight(QFont.Bold)
        p.setFont(f)
        p.drawText(QRectF(0, 0, rs, rs), Qt.AlignCenter, "PO")
    p.end()
    return pm.scaled(size, size, Qt.KeepAspectRatio, Qt.SmoothTransformation)


# ---------------------------------------------------------------------------
# Global stylesheet (minimal — most styling done via QPainter / QFont)
# ---------------------------------------------------------------------------

_STYLESHEET = f"""
QMainWindow, QWidget {{
    background-color: transparent;
    color: {_C_TEXT};
}}
QLabel#brand {{
    color: {_C_TEXT};
    font-size: {_SZ_BRAND}px;
    font-weight: 700;
    letter-spacing: 3px;
}}
QLabel#brandSub {{
    color: {_C_LIME};
    font-size: 10px;
    letter-spacing: 3px;
}}
QLabel#appTag {{
    color: {_C_MUTED};
    font-size: 10px;
    letter-spacing: 2px;
    border: 1px solid {_C_LINE};
    border-radius: 12px;
    padding: 5px 14px;
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
    font-size: 12px;
    letter-spacing: 4px;
}}
QLabel#designation {{
    color: {_C_TEXT};
    font-size: {_SZ_DESG}px;
    font-weight: 600;
}}
QLabel#priceNum {{
    color: {_C_LIME};
    font-size: {_SZ_PRICE_NUM}px;
    font-weight: 700;
}}
QLabel#priceCur {{
    color: {_C_LIME_DARK};
    font-size: {_SZ_PRICE_DA}px;
    font-weight: 600;
}}
QLabel#ref {{
    color: {_C_MUTED};
    font-size: {_SZ_REF}px;
    letter-spacing: 2px;
}}
QLabel#notFound {{
    color: {_C_DANGER};
    font-size: {_SZ_ERROR}px;
    font-weight: 700;
}}
QLabel#error {{
    color: {_C_DANGER};
    font-size: {_SZ_ERROR}px;
    font-weight: 600;
}}
QFrame#divider {{
    background-color: {_C_LINE};
    max-height: 1px;
    min-height: 1px;
}}
"""

# ---------------------------------------------------------------------------
# Animated logo mark — gentle rock + breathing lime aura (50 fps)
# ---------------------------------------------------------------------------

class _PulsingIcon(QWidget):
    """Prime Office logo mark that slowly rocks ±7° with a breathing lime glow."""

    _LOGO_SIZE = 190   # rendered logo square (px)
    _PAD       = 70    # extra space around it for the glow

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        side = self._LOGO_SIZE + self._PAD
        self.setFixedSize(side, side)
        # Pre-render crisp logo once — reused every frame
        self._logo = _make_logo_pixmap(self._LOGO_SIZE)
        self._phase = 0.0
        t = QTimer(self)
        t.timeout.connect(self._tick)
        t.start(20)  # 50 fps

    def _tick(self) -> None:
        self._phase += 0.022   # full cycle ≈ 4.7 s
        self.update()

    def paintEvent(self, event):  # type: ignore[override]
        pulse = 0.5 + 0.5 * math.sin(self._phase)          # 0 → 1, smooth
        rock  = math.sin(self._phase * 0.38) * 7.0          # ±7° gentle sway

        w = h = self.width()
        cx = cy = w / 2.0
        ls = self._LOGO_SIZE

        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setRenderHint(QPainter.SmoothPixmapTransform)

        # ── outer ambient halo (large, soft, breathes) ─────────────────────
        g_out = QRadialGradient(cx, cy, w * 0.50)
        g_out.setColorAt(0.25, QColor(198, 244, 50, int(10 + pulse * 20)))
        g_out.setColorAt(1.00, QColor(198, 244, 50, 0))
        outer = QPainterPath()
        outer.addEllipse(QRectF(0, 0, w, h))
        p.fillPath(outer, g_out)

        # ── inner glow (tighter, brighter, breathes more) ──────────────────
        r_in = ls * 0.64
        g_in = QRadialGradient(cx, cy, r_in)
        g_in.setColorAt(0.0, QColor(198, 244, 50, int(60 + pulse * 100)))
        g_in.setColorAt(1.0, QColor(198, 244, 50, 0))
        inner = QPainterPath()
        inner.addEllipse(QRectF(cx - r_in, cy - r_in, r_in * 2, r_in * 2))
        p.fillPath(inner, g_in)

        # ── logo mark, rotated around its own centre ────────────────────────
        p.save()
        p.translate(cx, cy)
        p.rotate(rock)
        p.drawPixmap(int(-ls / 2), int(-ls / 2), self._logo)
        p.restore()

        p.end()


# ---------------------------------------------------------------------------
# Shared brand bar — horizontal, lime underline
# ---------------------------------------------------------------------------

class _BrandBar(QWidget):
    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setFixedHeight(72)

        hl = QHBoxLayout(self)
        hl.setContentsMargins(28, 0, 28, 2)
        hl.setSpacing(12)
        hl.setAlignment(Qt.AlignVCenter)

        logo_lbl = QLabel()
        logo_lbl.setAlignment(Qt.AlignCenter)
        logo_lbl.setFixedSize(44, 44)
        logo_lbl.setPixmap(_make_logo_pixmap(44))
        hl.addWidget(logo_lbl)

        text_col = QWidget()
        tl = QVBoxLayout(text_col)
        tl.setContentsMargins(0, 0, 0, 0)
        tl.setSpacing(2)

        name_lbl = QLabel("PRIME OFFICE")
        name_lbl.setObjectName("brand")
        f_name = QFont(_FONT_HEADING)
        f_name.setPixelSize(_SZ_BRAND)
        f_name.setWeight(QFont.Bold)
        name_lbl.setFont(f_name)

        city_lbl = QLabel("ORAN  ·  ALGÉRIE")
        city_lbl.setObjectName("brandSub")
        f_city = QFont(_FONT_BODY)
        f_city.setPixelSize(10)
        f_city.setWeight(QFont.Medium)
        city_lbl.setFont(f_city)

        tl.addWidget(name_lbl)
        tl.addWidget(city_lbl)
        hl.addWidget(text_col)
        hl.addStretch()

        app_lbl = QLabel("VÉRIFICATEUR DE PRIX")
        app_lbl.setObjectName("appTag")
        f_app = QFont(_FONT_BODY)
        f_app.setPixelSize(10)
        app_lbl.setFont(f_app)
        hl.addWidget(app_lbl)

    def paintEvent(self, event):  # type: ignore[override]
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        grad = QLinearGradient(0, 0, 0, self.height())
        grad.setColorAt(0, QColor("#1A1048"))
        grad.setColorAt(1, QColor("#120D35"))
        p.fillRect(self.rect(), grad)
        pen = QPen(QColor(_C_LIME))
        pen.setWidth(2)
        p.setPen(pen)
        p.drawLine(0, self.height() - 1, self.width(), self.height() - 1)
        p.end()
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

        center = QWidget()
        vl = QVBoxLayout(center)
        vl.setAlignment(Qt.AlignCenter)
        vl.setSpacing(0)
        vl.setContentsMargins(80, 0, 80, 0)

        vl.addStretch(2)

        self._icon = _PulsingIcon()
        vl.addWidget(self._icon, alignment=Qt.AlignCenter)

        vl.addSpacing(52)

        lbl_fr = QLabel(i18n.SCAN_PROMPT_FR)
        lbl_fr.setObjectName("promptFr")
        lbl_fr.setAlignment(Qt.AlignCenter)
        lbl_fr.setWordWrap(True)
        f_fr = QFont(_FONT_BODY)
        f_fr.setPixelSize(_SZ_PROMPT_FR)
        f_fr.setWeight(QFont.Medium)
        lbl_fr.setFont(f_fr)
        vl.addWidget(lbl_fr)

        vl.addSpacing(16)

        lbl_ar = QLabel(i18n.SCAN_PROMPT_AR)
        lbl_ar.setObjectName("promptAr")
        lbl_ar.setAlignment(Qt.AlignCenter)
        lbl_ar.setLayoutDirection(Qt.RightToLeft)
        lbl_ar.setWordWrap(True)
        f_ar = QFont(_FONT_ARABIC)
        f_ar.setPixelSize(_SZ_PROMPT_AR)
        lbl_ar.setFont(f_ar)
        vl.addWidget(lbl_ar)

        vl.addSpacing(44)

        hint = QLabel("━━  SCANNER UN ARTICLE  ━━")
        hint.setObjectName("scanHint")
        hint.setAlignment(Qt.AlignCenter)
        f_hint = QFont(_FONT_BODY)
        f_hint.setPixelSize(12)
        hint.setFont(f_hint)
        vl.addWidget(hint)

        vl.addStretch(3)
        outer.addWidget(center, stretch=1)

    def paintEvent(self, event):  # type: ignore[override]
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        w, h = self.width(), self.height()

        p.fillRect(self.rect(), QColor(_C_BG))

        # Hero gradient: vivid purple top → deep dark
        hero = QLinearGradient(w / 2, 0, w / 2, h * 0.8)
        hero.setColorAt(0.0, QColor("#26145E"))
        hero.setColorAt(0.5, QColor(_C_BG2))
        hero.setColorAt(1.0, QColor(_C_BG))
        p.fillRect(self.rect(), hero)

        # Violet radial — top-right
        g1 = QRadialGradient(w * 0.92, 0, w * 0.75)
        g1.setColorAt(0.0, QColor(91, 46, 229, 110))
        g1.setColorAt(1.0, QColor(0, 0, 0, 0))
        p.fillRect(self.rect(), g1)

        # Purple radial — top-left
        g2 = QRadialGradient(0, 0, w * 0.60)
        g2.setColorAt(0.0, QColor(60, 30, 140, 75))
        g2.setColorAt(1.0, QColor(0, 0, 0, 0))
        p.fillRect(self.rect(), g2)

        # Lime radial — bottom center (very subtle)
        g3 = QRadialGradient(w / 2, h, w * 0.45)
        g3.setColorAt(0.0, QColor(198, 244, 50, 22))
        g3.setColorAt(1.0, QColor(0, 0, 0, 0))
        p.fillRect(self.rect(), g3)

        p.end()
        super().paintEvent(event)


# ---------------------------------------------------------------------------
# Result screen — full-screen, no card box, giant price
# ---------------------------------------------------------------------------

class _ResultScreen(QWidget):
    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        outer.addWidget(_BrandBar())

        # Content area — we animate its opacity for the pop-in effect
        self._content = QWidget()
        content_vl = QVBoxLayout(self._content)
        content_vl.setAlignment(Qt.AlignCenter)
        content_vl.setSpacing(0)
        content_vl.setContentsMargins(100, 0, 100, 0)

        content_vl.addStretch(1)

        # ── Designation ────────────────────────────────────────────────────
        self._designation = QLabel()
        self._designation.setObjectName("designation")
        self._designation.setAlignment(Qt.AlignCenter)
        self._designation.setWordWrap(True)
        f_d = QFont(_FONT_BODY)
        f_d.setPixelSize(_SZ_DESG)
        f_d.setWeight(QFont.DemiBold)
        self._designation.setFont(f_d)
        content_vl.addWidget(self._designation)

        content_vl.addSpacing(36)

        # ── Thin lime divider ───────────────────────────────────────────────
        self._divider = QFrame()
        self._divider.setObjectName("divider")
        self._divider.setFrameShape(QFrame.HLine)
        content_vl.addWidget(self._divider)

        content_vl.addSpacing(36)

        # ── Price row: [BIG NUMBER]  [DA] ──────────────────────────────────
        price_row = QWidget()
        price_hl = QHBoxLayout(price_row)
        price_hl.setAlignment(Qt.AlignCenter)
        price_hl.setSpacing(12)

        self._price_num = QLabel()
        self._price_num.setObjectName("priceNum")
        self._price_num.setAlignment(Qt.AlignVCenter | Qt.AlignRight)
        f_pn = QFont(_FONT_HEADING)
        f_pn.setPixelSize(_SZ_PRICE_NUM)
        f_pn.setWeight(QFont.Bold)
        self._price_num.setFont(f_pn)
        price_hl.addWidget(self._price_num)

        self._price_cur = QLabel()
        self._price_cur.setObjectName("priceCur")
        self._price_cur.setAlignment(Qt.AlignBottom | Qt.AlignLeft)
        f_pc = QFont(_FONT_BODY)
        f_pc.setPixelSize(_SZ_PRICE_DA)
        f_pc.setWeight(QFont.DemiBold)
        self._price_cur.setFont(f_pc)
        # Extra bottom padding to align baseline with number
        self._price_cur.setContentsMargins(0, 0, 0, int(_SZ_PRICE_NUM * 0.12))
        price_hl.addWidget(self._price_cur)

        content_vl.addWidget(price_row)

        content_vl.addSpacing(20)

        # ── Ref ─────────────────────────────────────────────────────────────
        self._ref = QLabel()
        self._ref.setObjectName("ref")
        self._ref.setAlignment(Qt.AlignCenter)
        f_r = QFont(_FONT_BODY)
        f_r.setPixelSize(_SZ_REF)
        self._ref.setFont(f_r)
        content_vl.addWidget(self._ref)

        # ── Error labels (not-found / db-error) ─────────────────────────────
        self._error_fr = QLabel()
        self._error_fr.setObjectName("error")
        self._error_fr.setAlignment(Qt.AlignCenter)
        self._error_fr.setWordWrap(True)
        f_e = QFont(_FONT_BODY)
        f_e.setPixelSize(_SZ_ERROR)
        f_e.setWeight(QFont.DemiBold)
        self._error_fr.setFont(f_e)

        self._error_ar = QLabel()
        self._error_ar.setObjectName("error")
        self._error_ar.setAlignment(Qt.AlignCenter)
        self._error_ar.setLayoutDirection(Qt.RightToLeft)
        self._error_ar.setWordWrap(True)
        f_ar = QFont(_FONT_ARABIC)
        f_ar.setPixelSize(_SZ_ERROR)
        self._error_ar.setFont(f_ar)

        content_vl.addWidget(self._error_fr)
        content_vl.addSpacing(6)
        content_vl.addWidget(self._error_ar)

        content_vl.addStretch(2)
        outer.addWidget(self._content, stretch=1)

        # ── Lime glow on price number ───────────────────────────────────────
        price_glow = QGraphicsDropShadowEffect()
        price_glow.setBlurRadius(80)
        price_glow.setColor(QColor(198, 244, 50, 220))
        price_glow.setOffset(0, 0)
        self._price_num.setGraphicsEffect(price_glow)

        # ── Opacity effect for pop-in animation ─────────────────────────────
        self._opacity_fx = QGraphicsOpacityEffect()
        self._opacity_fx.setOpacity(1.0)
        self._content.setGraphicsEffect(self._opacity_fx)

        self._fade_in = QPropertyAnimation(self._opacity_fx, b"opacity", self)
        self._fade_in.setDuration(380)
        self._fade_in.setStartValue(0.0)
        self._fade_in.setEndValue(1.0)
        self._fade_in.setEasingCurve(QEasingCurve.OutCubic)

    def _animate_in(self) -> None:
        self._fade_in.stop()
        self._opacity_fx.setOpacity(0.0)
        self._fade_in.start()

    def paintEvent(self, event):  # type: ignore[override]
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        w, h = self.width(), self.height()

        p.fillRect(self.rect(), QColor(_C_BG))

        hero = QLinearGradient(w / 2, 0, w / 2, h)
        hero.setColorAt(0.0, QColor("#221058"))
        hero.setColorAt(0.4, QColor(_C_BG2))
        hero.setColorAt(1.0, QColor(_C_BG))
        p.fillRect(self.rect(), hero)

        g1 = QRadialGradient(w, 0, w * 0.80)
        g1.setColorAt(0.0, QColor(91, 46, 229, 95))
        g1.setColorAt(1.0, QColor(0, 0, 0, 0))
        p.fillRect(self.rect(), g1)

        g2 = QRadialGradient(0, h, w * 0.55)
        g2.setColorAt(0.0, QColor(198, 244, 50, 28))
        g2.setColorAt(1.0, QColor(0, 0, 0, 0))
        p.fillRect(self.rect(), g2)

        p.end()
        super().paintEvent(event)

    # --- public API called by KioskWindow ----------------------------------

    def show_article(self, designation: str, prix: float, ref_art: str = "") -> None:
        # Split "12 500,00 DA" → num part + currency part
        formatted = i18n.format_price(prix)
        parts = formatted.rsplit(" ", 1)
        price_num = parts[0]
        price_cur = parts[1] if len(parts) > 1 else i18n.CURRENCY

        self._designation.setText(designation.upper())
        self._price_num.setText(price_num)
        self._price_cur.setText(price_cur)
        self._ref.setText(f"RÉF : {ref_art.upper()}" if ref_art else "")

        self._designation.show()
        self._divider.show()
        self._price_num.show()
        self._price_cur.show()
        self._ref.show()
        self._error_fr.hide()
        self._error_ar.hide()
        self._animate_in()

    def show_not_found(self) -> None:
        self._designation.hide()
        self._divider.hide()
        self._price_num.hide()
        self._price_cur.hide()
        self._ref.hide()
        self._error_fr.setObjectName("notFound")
        self._error_ar.setObjectName("notFound")
        self._error_fr.setText(i18n.NOT_FOUND_FR)
        self._error_ar.setText(i18n.NOT_FOUND_AR)
        self._error_fr.show()
        self._error_ar.show()
        self._animate_in()

    def show_db_error(self) -> None:
        self._designation.hide()
        self._divider.hide()
        self._price_num.hide()
        self._price_cur.hide()
        self._ref.hide()
        self._error_fr.setObjectName("error")
        self._error_ar.setObjectName("error")
        self._error_fr.setText(i18n.DB_ERROR_FR)
        self._error_ar.setText(i18n.DB_ERROR_AR)
        self._error_fr.show()
        self._error_ar.show()
        self._animate_in()

    def set_image(self, path: Optional[str]) -> None:
        # Image display removed from main layout — no-op (WooCommerce not displayed)
        pass


# ---------------------------------------------------------------------------
# Async workers
# ---------------------------------------------------------------------------

class _LookupWorker(QObject):
    finished = Signal(str, object, object)  # scan_id, article|None, error|None

    def __init__(self, cfg, code: str, scan_id: str):
        super().__init__()
        self._cfg = cfg
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
# Main kiosk window
# ---------------------------------------------------------------------------

class KioskWindow(QMainWindow):
    """Frameless fullscreen price-checker kiosk."""

    def __init__(self, cfg: AppConfig, db: Database, woo: WooClient,
                 parent: Optional[QWidget] = None):
        super().__init__(parent)
        self._cfg = cfg
        self._db = db
        self._woo = woo

        self._current_scan_id: Optional[str] = None
        self._lookup_thread: Optional[QThread] = None
        self._img_thread: Optional[QThread] = None

        self.setWindowFlags(Qt.FramelessWindowHint | Qt.Window)
        self.setStyleSheet(_STYLESHEET)

        central = QWidget()
        self.setCentralWidget(central)
        central.setAutoFillBackground(False)
        outer = QVBoxLayout(central)
        outer.setContentsMargins(0, 0, 0, 0)

        self._stack = QStackedWidget()
        self._idle_screen = _IdleScreen()
        self._result_screen = _ResultScreen()
        self._stack.addWidget(self._idle_screen)    # index 0
        self._stack.addWidget(self._result_screen)   # index 1
        outer.addWidget(self._stack)

        # Invisible barcode input field
        self._barcode_input = QLineEdit(central)
        self._barcode_input.setFixedSize(1, 1)
        self._barcode_input.move(-10, -10)
        self._barcode_input.setStyleSheet(
            "background: transparent; border: none; color: transparent;"
        )
        self._barcode_input.returnPressed.connect(self._on_barcode)
        self._barcode_input.installEventFilter(self)

        self._idle_timer = QTimer(self)
        self._idle_timer.setSingleShot(True)
        self._idle_timer.timeout.connect(self._show_idle)

        QShortcut(QKeySequence(cfg.ui.exit_hotkey), self).activated.connect(self.close)
        QShortcut(QKeySequence("Ctrl+Alt+S"), self).activated.connect(self._open_setup)

        self._show_idle()

    def showEvent(self, event):  # type: ignore[override]
        super().showEvent(event)
        if self._cfg.ui.fullscreen:
            self.showFullScreen()
        self._focus_barcode()

    def _focus_barcode(self) -> None:
        self._barcode_input.setFocus(Qt.OtherFocusReason)

    def eventFilter(self, obj, event):  # type: ignore[override]
        from PySide6.QtCore import QEvent
        if obj is self._barcode_input and event.type() == QEvent.FocusOut:
            QTimer.singleShot(50, self._focus_barcode)
        return super().eventFilter(obj, event)

    def mousePressEvent(self, event):  # type: ignore[override]
        self._focus_barcode()
        super().mousePressEvent(event)

    # --- Screen transitions ------------------------------------------------

    def _show_idle(self) -> None:
        self._idle_timer.stop()
        self._current_scan_id = None
        self._stack.setCurrentIndex(0)
        self._focus_barcode()

    def _show_result(self) -> None:
        self._stack.setCurrentIndex(1)
        self._idle_timer.start(self._cfg.ui.idle_reset_seconds * 1000)
        self._focus_barcode()

    # --- Barcode handling --------------------------------------------------

    def _on_barcode(self) -> None:
        code = self._barcode_input.text().strip()
        self._barcode_input.clear()
        if not code:
            return

        self._idle_timer.stop()
        if self._lookup_thread is not None and self._lookup_thread.isRunning():
            self._lookup_thread.quit()

        scan_id = str(uuid.uuid4())
        self._current_scan_id = scan_id
        # Return to idle screen WITHOUT calling _show_idle() — that would reset scan_id
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
        if scan_id != self._current_scan_id:
            return

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
            art.prix_vente_ht,   # raw float — result screen handles formatting
            art.ref_art,
        )
        self._show_result()
        self._start_image_fetch(art.ref_art, scan_id)

    # --- Async image loading (kept for future WooCommerce integration) ------

    def _start_image_fetch(self, ref_art: str, scan_id: str) -> None:
        if self._img_thread is not None and self._img_thread.isRunning():
            self._img_thread.quit()
        thread = QThread(self)
        worker = _ImageWorker(self._woo, ref_art, scan_id)
        worker.moveToThread(thread)
        worker.finished.connect(self._on_image_ready)
        thread.started.connect(worker.run)
        thread.finished.connect(thread.deleteLater)
        self._img_thread = thread
        self._img_worker = worker  # type: ignore[attr-defined]
        thread.start()

    def _on_image_ready(self, scan_id: str, path: Optional[str]) -> None:
        if scan_id != self._current_scan_id:
            return
        self._result_screen.set_image(path)

    # --- Settings dialog ---------------------------------------------------

    def _open_setup(self) -> None:
        from ui.setup_window import SetupDialog
        from config import load
        cfg = load()
        dlg = SetupDialog(cfg, parent=self)
        if dlg.exec():
            self._cfg = load()
