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
    QSize,
    Qt,
    QThread,
    QTimer,
    Signal,
)
from PySide6.QtGui import (
    QColor,
    QFont,
    QKeySequence,
    QPainter,
    QPixmap,
)
from PySide6.QtWidgets import (
    QApplication,
    QLabel,
    QLineEdit,
    QMainWindow,
    QShortcut,
    QSizePolicy,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

import i18n
from config import AppConfig
from database import Database, DatabaseError
from woocommerce import WooClient

# Chemin de l'image de remplacement (dans le dossier assets/ à la racine du projet)
_ASSETS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                           "assets")
_PLACEHOLDER_PATH = os.path.join(_ASSETS_DIR, "placeholder.png")

# Dimensions d'affichage des images produit
_IMAGE_MAX_W = 400
_IMAGE_MAX_H = 400

# Taille des polices
_FONT_PROMPT_FR_PX = 36
_FONT_PROMPT_AR_PX = 44
_FONT_DESIGNATION_PX = 40
_FONT_PRICE_PX = 72
_FONT_ERROR_PX = 32

# Couleurs du thème (fond sombre, texte clair — bon contraste kiosque)
_COLOR_BG = "#1a1a2e"
_COLOR_TEXT = "#eaeaea"
_COLOR_PRICE = "#f5c518"
_COLOR_ERROR = "#e05c5c"
_COLOR_NOT_FOUND = "#f0a500"

# Feuille de style globale de la fenêtre kiosque
_STYLESHEET = f"""
QMainWindow, QWidget#centralWidget {{
    background-color: {_COLOR_BG};
}}
QLabel#promptFr {{
    color: {_COLOR_TEXT};
    font-size: {_FONT_PROMPT_FR_PX}px;
}}
QLabel#promptAr {{
    color: {_COLOR_TEXT};
    font-size: {_FONT_PROMPT_AR_PX}px;
}}
QLabel#designation {{
    color: {_COLOR_TEXT};
    font-size: {_FONT_DESIGNATION_PX}px;
    font-weight: bold;
}}
QLabel#price {{
    color: {_COLOR_PRICE};
    font-size: {_FONT_PRICE_PX}px;
    font-weight: bold;
}}
QLabel#image {{
    background-color: transparent;
}}
QLabel#error {{
    color: {_COLOR_ERROR};
    font-size: {_FONT_ERROR_PX}px;
    font-weight: bold;
}}
QLabel#notFound {{
    color: {_COLOR_NOT_FOUND};
    font-size: {_FONT_ERROR_PX}px;
    font-weight: bold;
}}
"""


def _make_placeholder_pixmap(w: int = _IMAGE_MAX_W, h: int = _IMAGE_MAX_H) -> QPixmap:
    """Génère un QPixmap gris neutre en guise d'image manquante."""
    pm = QPixmap(w, h)
    pm.fill(QColor("#3a3a4e"))
    painter = QPainter(pm)
    painter.setPen(QColor("#777788"))
    font = QFont()
    font.setPixelSize(22)
    painter.setFont(font)
    painter.drawText(pm.rect(), Qt.AlignCenter, "Image\nnon disponible")
    painter.end()
    return pm


def _load_placeholder() -> QPixmap:
    """Charge placeholder.png ou génère un pixmap de secours."""
    if os.path.exists(_PLACEHOLDER_PATH):
        pm = QPixmap(_PLACEHOLDER_PATH)
        if not pm.isNull():
            return pm.scaled(_IMAGE_MAX_W, _IMAGE_MAX_H,
                             Qt.KeepAspectRatio, Qt.SmoothTransformation)
    return _make_placeholder_pixmap()


# ---------------------------------------------------------------------------
# Worker de chargement d'image (thread secondaire)
# ---------------------------------------------------------------------------

class _ImageWorker(QObject):
    """Récupère l'image produit hors du thread principal."""

    # Émet (scan_id, chemin_local_ou_None)
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
# Écran de repos (idle)
# ---------------------------------------------------------------------------

class _IdleScreen(QWidget):
    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setAlignment(Qt.AlignCenter)
        layout.setSpacing(30)

        # Invite en français
        self._label_fr = QLabel(i18n.SCAN_PROMPT_FR)
        self._label_fr.setObjectName("promptFr")
        self._label_fr.setAlignment(Qt.AlignCenter)
        self._label_fr.setWordWrap(True)
        layout.addWidget(self._label_fr)

        # Invite en arabe (RTL, police avec support Unicode arabe)
        self._label_ar = QLabel(i18n.SCAN_PROMPT_AR)
        self._label_ar.setObjectName("promptAr")
        self._label_ar.setAlignment(Qt.AlignCenter)
        self._label_ar.setLayoutDirection(Qt.RightToLeft)
        self._label_ar.setWordWrap(True)

        # Police avec bon support arabe : Noto Arabic si disponible, sinon fallback
        ar_font = QFont("Noto Naskh Arabic, Noto Sans Arabic, Arabic Typesetting, Arial")
        ar_font.setPixelSize(_FONT_PROMPT_AR_PX)
        self._label_ar.setFont(ar_font)

        layout.addWidget(self._label_ar)


# ---------------------------------------------------------------------------
# Écran résultat (article trouvé / non trouvé / erreur)
# ---------------------------------------------------------------------------

class _ResultScreen(QWidget):
    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setAlignment(Qt.AlignCenter)
        layout.setSpacing(16)

        # Image produit
        self._image_label = QLabel()
        self._image_label.setObjectName("image")
        self._image_label.setAlignment(Qt.AlignCenter)
        self._image_label.setFixedSize(_IMAGE_MAX_W, _IMAGE_MAX_H)
        layout.addWidget(self._image_label, alignment=Qt.AlignCenter)

        # Nom de l'article
        self._designation_label = QLabel()
        self._designation_label.setObjectName("designation")
        self._designation_label.setAlignment(Qt.AlignCenter)
        self._designation_label.setWordWrap(True)
        layout.addWidget(self._designation_label)

        # Prix
        self._price_label = QLabel()
        self._price_label.setObjectName("price")
        self._price_label.setAlignment(Qt.AlignCenter)
        layout.addWidget(self._price_label)

        # Message erreur / non trouvé (masqué par défaut)
        self._error_fr = QLabel()
        self._error_fr.setObjectName("error")
        self._error_fr.setAlignment(Qt.AlignCenter)
        self._error_fr.setWordWrap(True)

        self._error_ar = QLabel()
        self._error_ar.setObjectName("error")
        self._error_ar.setAlignment(Qt.AlignCenter)
        self._error_ar.setLayoutDirection(Qt.RightToLeft)
        self._error_ar.setWordWrap(True)
        ar_font = QFont("Noto Naskh Arabic, Noto Sans Arabic, Arabic Typesetting, Arial")
        ar_font.setPixelSize(_FONT_ERROR_PX)
        self._error_ar.setFont(ar_font)

        layout.addWidget(self._error_fr)
        layout.addWidget(self._error_ar)

        self._placeholder = _load_placeholder()

    def show_article(self, designation: str, prix_str: str) -> None:
        """Affiche le nom et le prix ; l'image sera mise à jour ensuite."""
        self._image_label.setPixmap(self._placeholder)
        self._designation_label.setText(designation)
        self._price_label.setText(prix_str)
        self._designation_label.show()
        self._price_label.show()
        self._error_fr.hide()
        self._error_ar.hide()

    def set_image(self, path: Optional[str]) -> None:
        """Remplace l'image par celle téléchargée (appelé depuis le thread principal)."""
        if path and os.path.exists(path):
            pm = QPixmap(path)
            if not pm.isNull():
                pm = pm.scaled(_IMAGE_MAX_W, _IMAGE_MAX_H,
                               Qt.KeepAspectRatio, Qt.SmoothTransformation)
                self._image_label.setPixmap(pm)
                return
        # Conserve le placeholder en cas d'échec
        self._image_label.setPixmap(self._placeholder)

    def show_not_found(self) -> None:
        self._image_label.setPixmap(self._placeholder)
        self._designation_label.hide()
        self._price_label.hide()
        self._error_fr.setObjectName("notFound")
        self._error_ar.setObjectName("notFound")
        self._error_fr.setText(i18n.NOT_FOUND_FR)
        self._error_ar.setText(i18n.NOT_FOUND_AR)
        self._error_fr.show()
        self._error_ar.show()

    def show_db_error(self) -> None:
        self._image_label.setPixmap(self._placeholder)
        self._designation_label.hide()
        self._price_label.hide()
        self._error_fr.setObjectName("error")
        self._error_ar.setObjectName("error")
        self._error_fr.setText(i18n.DB_ERROR_FR)
        self._error_ar.setText(i18n.DB_ERROR_AR)
        self._error_fr.show()
        self._error_ar.show()


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
