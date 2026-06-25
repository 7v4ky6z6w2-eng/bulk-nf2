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
)
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
    color: {_C_LIME};
    font-size: {_SZ_ERROR}px;
    font-weight: 600;
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
# Worker de chargement d'image
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
        self.setFixedHeight(64)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(40, 0, 40, 0)

        lbl = QLabel("PRIME OFFICE")
        lbl.setObjectName("brand")
        f = QFont(_FONT_HEADING)
        f.setPixelSize(_SZ_BRAND)
        f.setWeight(QFont.Bold)
        lbl.setFont(f)
        layout.addWidget(lbl)
        layout.addStretch()

        tagline = QLabel("Oran · Algérie")
        tagline.setObjectName("scanHint")
        f2 = QFont(_FONT_BODY)
        f2.setPixelSize(14)
        tagline.setFont(f2)
        layout.addWidget(tagline)

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

        # Identifiant du scan en cours (pour ignorer les images en retard)
        self._current_scan_id: Optional[str] = None

        # Thread de chargement d'image
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

        # Annule la minuterie en cours (nouveau scan repart de zéro)
        self._idle_timer.stop()

        # Identifiant unique pour ce scan (protège contre les images en retard)
        scan_id = str(uuid.uuid4())
        self._current_scan_id = scan_id

        try:
            article = self._db.lookup_article(code)
        except DatabaseError:
            self._result_screen.show_db_error()
            self._show_result()
            return

        if article is None:
            self._result_screen.show_not_found()
            self._show_result()
            return

        # Article trouvé : affichage immédiat nom + prix
        self._result_screen.show_article(
            article.designation,
            i18n.format_price(article.prix_vente_ht),
            article.ref_art,
        )
        self._show_result()

        # Chargement asynchrone de l'image
        self._start_image_fetch(article.ref_art, scan_id)

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
