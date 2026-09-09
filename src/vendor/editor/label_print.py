# -*- coding: utf-8 -*-
"""
label_print.py
==============

Impression d'etiquettes articles pour PRIME, **directement** sur une
imprimante choisie (sans fichier PDF/HTML intermediaire), sous Windows via
l'API GDI (pywin32). Le meme code de mise en page sert aussi a l'apercu a
l'ecran (rendu sur un Canvas Tkinter), pour verifier avant d'imprimer.

Trois modeles d'etiquettes :

  * ``M1`` 40x20 mm : code-barres + designation + prix
  * ``M2`` 80x20 mm : designation + prix (sans code-barres)
  * ``M3`` 40x20 mm : prix normal barre + prix promo (ticket de remise)

Le code-barres est un **Code 128** (variante B) genere en Python pur, donc
scannable par n'importe quelle douchette. Aucune dependance n'est requise
pour l'apercu ; l'impression reelle utilise ``pywin32`` (Windows).

Unites : toute la mise en page raisonne en **millimetres** (origine en haut
a gauche, y vers le bas). Chaque "renderer" convertit les mm vers ses propres
unites (pixels d'apercu, ou points-imprimante).
"""

from __future__ import annotations

from dataclasses import dataclass, field


# --------------------------------------------------------------------------- #
#  Code 128 (jeu B) — generation en Python pur
# --------------------------------------------------------------------------- #
#: Motifs Code 128 : pour chaque valeur (0..106), largeurs successives
#: barre/espace/barre/espace/barre/espace (le motif d'arret a 7 modules).
_CODE128_PATTERNS = [
    "212222", "222122", "222221", "121223", "121322", "131222", "122213",
    "122312", "132212", "221213", "221312", "231212", "112232", "122132",
    "122231", "113222", "123122", "123221", "223211", "221132", "221231",
    "213212", "223112", "312131", "311222", "321122", "321221", "312212",
    "322112", "322211", "212123", "212321", "232121", "111323", "131123",
    "131321", "112313", "132113", "132311", "211313", "231113", "231311",
    "112133", "112331", "132131", "113123", "113321", "133121", "313121",
    "211331", "231131", "213113", "213311", "213131", "311123", "311321",
    "331121", "312113", "312311", "332111", "314111", "221411", "431111",
    "111224", "111422", "121124", "121421", "141122", "141221", "112214",
    "112412", "122114", "122411", "142112", "142211", "241211", "221114",
    "413111", "241112", "134111", "111242", "121142", "121241", "114212",
    "124112", "124211", "411212", "421112", "421211", "212141", "214121",
    "412121", "111143", "111341", "131141", "114113", "114311", "411113",
    "411311", "113141", "114131", "311141", "411131", "211412", "211214",
    "211232", "2331112",
]
_START_B = 104
_START_C = 105
_SWITCH_B = 100          # bascule vers le jeu B depuis un autre jeu
_SWITCH_C = 99           # bascule vers le jeu C (chiffres par paires)
_STOP = 106


def _widths_from_symbols(symbols):
    """Ajoute la cle de controle + l'arret, puis convertit en largeurs."""
    total = symbols[0]
    for k, v in enumerate(symbols[1:], start=1):
        total += k * v
    full = symbols + [total % 103, _STOP]
    widths = []
    for s in full:
        widths.extend(int(ch) for ch in _CODE128_PATTERNS[s])
    return widths


def code128b_widths(data):
    """Encode ``data`` en Code 128B ; renvoie la liste des largeurs de modules.

    La liste alterne barre/espace en commençant par une **barre** (indice pair
    = barre noire, indice impair = espace blanc). Les caracteres hors ASCII
    imprimable (32..126) sont ignores.

    >>> w = code128b_widths("A")
    >>> w[0] > 0 and len(w) % 2 == 1     # commence et finit par une barre
    True
    """
    chars = [c for c in str(data) if 32 <= ord(c) <= 126]
    if not chars:
        chars = [" "]
    values = [ord(c) - 32 for c in chars]
    return _widths_from_symbols([_START_B] + values)


def _leading_digits(s, i):
    n = 0
    while i + n < len(s) and s[i + n].isdigit():
        n += 1
    return n


def code128_widths(data):
    """Encode ``data`` en Code 128 **auto** (jeux B et C) ; renvoie les largeurs.

    Les suites de chiffres sont encodees en jeu C (2 chiffres par symbole), ce
    qui reduit de moitie la largeur des codes numeriques (ex EAN) : les barres
    sont ~2x plus larges et donc bien plus lisibles sur une petite etiquette
    imprimee a 203 dpi. Le reste est encode en jeu B.
    """
    s = "".join(c for c in str(data) if 32 <= ord(c) <= 126) or " "
    symbols = []
    i = 0
    ld = _leading_digits(s, 0)
    if ld >= 4 or (ld == len(s) and ld >= 2 and ld % 2 == 0):
        mode = "C"
        symbols.append(_START_C)
    else:
        mode = "B"
        symbols.append(_START_B)
    while i < len(s):
        if mode == "C":
            if i + 1 < len(s) and s[i].isdigit() and s[i + 1].isdigit():
                symbols.append(int(s[i:i + 2]))
                i += 2
            else:
                symbols.append(_SWITCH_B)
                mode = "B"
        else:
            ld = _leading_digits(s, i)
            if ld >= 4 or (ld == len(s) - i and ld >= 2 and ld % 2 == 0):
                if ld % 2 == 1:                    # rendre la suite paire
                    symbols.append(ord(s[i]) - 32)
                    i += 1
                symbols.append(_SWITCH_C)
                mode = "C"
            else:
                symbols.append(ord(s[i]) - 32)
                i += 1
    return _widths_from_symbols(symbols)


def code128b_module_count(data):
    """Nombre total de modules (largeur en 'X') du code-barres, hors marges."""
    return sum(code128b_widths(data))
    """Encode ``data`` en Code 128B ; renvoie la liste des largeurs de modules.

    La liste alterne barre/espace en commençant par une **barre** (indice pair
    = barre noire, indice impair = espace blanc). Les caracteres hors ASCII
    imprimable (32..126) sont ignores.

    >>> w = code128b_widths("A")
    >>> w[0] > 0 and len(w) % 2 == 1     # commence et finit par une barre
    True
    """
    chars = [c for c in str(data) if 32 <= ord(c) <= 126]
    if not chars:
        chars = [" "]
    values = [ord(c) - 32 for c in chars]
    checksum = (_START_B + sum((i + 1) * v for i, v in enumerate(values))) % 103
    symbols = [_START_B] + values + [checksum, _STOP]
    widths = []
    for s in symbols:
        widths.extend(int(ch) for ch in _CODE128_PATTERNS[s])
    return widths


def code128b_module_count(data):
    """Nombre total de modules (largeur en 'X') du code-barres, hors marges."""
    return sum(code128b_widths(data))


# --------------------------------------------------------------------------- #
#  Modeles d'etiquettes
# --------------------------------------------------------------------------- #
@dataclass
class LabelModel:
    key: str
    name: str
    width_mm: float
    height_mm: float
    show_barcode: bool = False
    show_designation: bool = True
    price_mode: str = "normal"       # "normal" | "promo_discount"
    max_designation_chars: int = 0   # 0 = pas de limite


LABEL_MODELS = [
    LabelModel("M1", "Modele 1 — Prix (grand) + designation + code-barres (40x20 mm)",
               40, 20, show_barcode=True, show_designation=True,
               price_mode="normal", max_designation_chars=20),
    LabelModel("M2", "Modele 2 — Designation + prix (80x20 mm)",
               80, 20, show_barcode=False, show_designation=True,
               price_mode="normal", max_designation_chars=40),
    LabelModel("M3", "Modele 3 — Prix + prix promo / remise (40x20 mm)",
               40, 20, show_barcode=False, show_designation=True,
               price_mode="promo_discount", max_designation_chars=22),
]


def model_by_key(key):
    for m in LABEL_MODELS:
        if m.key == key:
            return m
    return LABEL_MODELS[0]


# --------------------------------------------------------------------------- #
#  Donnees d'un article a etiqueter
# --------------------------------------------------------------------------- #
@dataclass
class LabelItem:
    designation: str = ""
    reference: str = ""
    barcode: str = ""
    price: float | None = None          # prix de vente TTC "normal"
    promo: float | None = None          # prix promo TTC (None si pas de promo)
    currency: str = "DA"

    def price_text(self, value):
        if value is None:
            return ""
        return ("%.2f" % float(value)).replace(".", ",") + " " + self.currency


# --------------------------------------------------------------------------- #
#  Rendu abstrait (mm) — implemente par l'apercu Tk et l'impression GDI
# --------------------------------------------------------------------------- #
class LabelRenderer:
    """Interface de dessin en millimetres (origine haut-gauche, y vers le bas).

    hauteur de texte exprimee en mm (``h_mm``). ``anchor`` : coin/cote de
    reference du point (x, y) — combinaisons de n/s + w/e + centre ("center").
    """

    def text(self, x, y, s, h_mm, bold=False, anchor="nw", strike=False):
        raise NotImplementedError

    def measure(self, s, h_mm, bold=False):
        """Renvoie (largeur_mm, hauteur_mm) du texte."""
        raise NotImplementedError

    def rect(self, x, y, w, h):
        """Rectangle plein noir (barres de code-barres)."""
        raise NotImplementedError


# --------------------------------------------------------------------------- #
#  Mise en page partagee
# --------------------------------------------------------------------------- #
def layout_label(model, item, r):
    """Dessine une etiquette ``item`` selon ``model`` via le renderer ``r``.

    Toute la geometrie est en millimetres, bornee a
    ``model.width_mm`` x ``model.height_mm``.
    """
    W, H = model.width_mm, model.height_mm
    pad = 1.2
    cx = W / 2.0
    desig = _cap(item.designation, model.max_designation_chars)

    if model.price_mode == "promo_discount":
        _layout_discount(model, item, r, W, H, pad, cx, desig)
        return

    if model.show_barcode:
        # moitie HAUTE : designation (petit) puis PRIX en grand ;
        # moitie BASSE : code-barres + numero.
        half = H / 2.0
        y = pad
        if model.show_designation and desig:
            _fit_text(r, cx, y, desig, 2.6, W - 2 * pad, anchor="n")
            y += 2.9
        ptxt = item.price_text(item.price)
        if ptxt:
            ph = min(6.5, max(3.0, half - y))
            _fit_text(r, cx, (y + half) / 2.0, ptxt, ph, W - 2 * pad,
                      bold=True, anchor="center")
        by = half + 0.2
        bc_h = max(5.0, H - by - 2.6)
        _draw_barcode(r, item.barcode, pad, by, W - 2 * pad, bc_h)
        _fit_text(r, cx, by + bc_h + 0.1, item.barcode, 2.0, W - 2 * pad,
                  anchor="n")
    else:
        y = pad
        if model.show_designation and desig:
            _fit_text(r, cx, y, desig, 3.4, W - 2 * pad, bold=True, anchor="n")
            y += 4.0
        # grand prix centre dans l'espace restant
        ptxt = item.price_text(item.price)
        ph = min(9.0, (H - y) - pad)
        if ptxt:
            _fit_text(r, cx, y + (H - y - pad) / 2.0, ptxt, ph,
                      W - 2 * pad, bold=True, anchor="center")


def _layout_discount(model, item, r, W, H, pad, cx, desig=None):
    """Modele 3 : petit intitule, prix normal barre, prix promo en grand."""
    if desig is None:
        desig = item.designation
    y = pad
    if desig:
        _fit_text(r, cx, y, desig, 2.4, W - 2 * pad, anchor="n")
        y += 2.8

    normal = item.price_text(item.price)
    promo = item.price_text(item.promo if item.promo is not None else item.price)

    # prix normal barre (petit), au-dessus
    if normal:
        r.text(cx, y, normal, 3.0, bold=False, anchor="n", strike=True)
        y += 3.4
    # prix promo en grand, centre
    ph = min(7.5, (H - y) - pad)
    r.text(cx, y + (H - y - pad) / 2.0, promo, ph, bold=True, anchor="center")


def _cap(text, max_chars):
    """Tronque ``text`` a ``max_chars`` caracteres (0 = illimite)."""
    text = "" if text is None else str(text)
    if max_chars and len(text) > max_chars:
        return text[:max_chars].rstrip()
    return text


def _fit_text(r, x, y, text, h_mm, max_w, bold=False, anchor="n"):
    """Ecrit ``text`` a (x, y) avec l'ancrage ``anchor``, en reduisant la
    taille pour tenir dans ``max_w`` ; tronque si vraiment trop long."""
    text = str(text)
    if max_w <= 0:
        return
    h = h_mm
    for _ in range(6):
        w = r.measure(text, h, bold=bold)[0]
        if w <= max_w or h <= 1.4:
            break
        h *= 0.9
    while r.measure(text, h, bold=bold)[0] > max_w and len(text) > 1:
        text = text[:-1]
    r.text(x, y, text, h, bold=bold, anchor=anchor)


def _draw_barcode(r, data, x, y, w, h, quiet=10):
    """Dessine un Code 128 (auto B/C) dans le rectangle (x, y, w, h) en mm."""
    widths = code128_widths(data)
    total = sum(widths) + 2 * quiet
    module = w / float(total)          # largeur d'un module en mm
    cursor = x + quiet * module        # marge silencieuse a gauche
    for i, mw in enumerate(widths):
        bw = mw * module
        if i % 2 == 0:                 # indice pair = barre noire
            r.rect(cursor, y, bw, h)
        cursor += bw


# --------------------------------------------------------------------------- #
#  Renderer d'APERCU sur Canvas Tkinter
# --------------------------------------------------------------------------- #
class TkCanvasRenderer(LabelRenderer):
    """Dessine l'etiquette sur un ``tkinter.Canvas`` (apercu a l'ecran)."""

    def __init__(self, canvas, scale, ox=0, oy=0, family="Arial"):
        self.c = canvas
        self.s = float(scale)          # pixels par mm
        self.ox = ox
        self.oy = oy
        self.family = family
        self._font = None
        import tkinter.font as tkfont
        self._tkfont = tkfont

    def _font_for(self, h_mm, bold):
        size = max(1, int(round(h_mm * self.s)))
        return self._tkfont.Font(family=self.family, size=-size,
                                 weight="bold" if bold else "normal")

    def _xy(self, x, y):
        return self.ox + x * self.s, self.oy + y * self.s

    _ANCHOR = {"nw": "nw", "n": "n", "ne": "ne", "center": "center",
               "w": "w", "e": "e", "sw": "sw", "s": "s", "se": "se"}

    def text(self, x, y, s, h_mm, bold=False, anchor="nw", strike=False):
        f = self._font_for(h_mm, bold)
        if strike:
            f.configure(overstrike=1)
        px, py = self._xy(x, y)
        self.c.create_text(px, py, text=s, font=f,
                           anchor=self._ANCHOR.get(anchor, "nw"), fill="black")

    def measure(self, s, h_mm, bold=False):
        f = self._font_for(h_mm, bold)
        w = f.measure(str(s)) / self.s
        return w, h_mm

    def rect(self, x, y, w, h):
        px, py = self._xy(x, y)
        self.c.create_rectangle(px, py, px + w * self.s, py + h * self.s,
                                fill="black", width=0)


# --------------------------------------------------------------------------- #
#  Renderer de test (enregistre les primitives, sans affichage)
# --------------------------------------------------------------------------- #
class RecordingRenderer(LabelRenderer):
    """Enregistre les operations de dessin (pour tests unitaires)."""

    def __init__(self, char_ratio=0.6):
        self.texts = []
        self.rects = []
        self.ratio = char_ratio

    def text(self, x, y, s, h_mm, bold=False, anchor="nw", strike=False):
        self.texts.append(dict(x=x, y=y, s=s, h=h_mm, bold=bold,
                               anchor=anchor, strike=strike))

    def measure(self, s, h_mm, bold=False):
        return len(str(s)) * h_mm * self.ratio, h_mm

    def rect(self, x, y, w, h):
        self.rects.append((x, y, w, h))


# --------------------------------------------------------------------------- #
#  Impression reelle (Windows / GDI via pywin32)
# --------------------------------------------------------------------------- #
def printing_available():
    """True si l'impression directe est possible (Windows + pywin32)."""
    try:
        import win32ui  # noqa: F401
        import win32print  # noqa: F401
        return True
    except Exception:      # noqa: BLE001
        return False


def list_printers():
    """Liste des noms d'imprimantes installees (vide hors Windows)."""
    try:
        import win32print
        flags = (win32print.PRINTER_ENUM_LOCAL |
                 win32print.PRINTER_ENUM_CONNECTIONS)
        return [p[2] for p in win32print.EnumPrinters(flags)]
    except Exception:      # noqa: BLE001
        return []


def default_printer():
    try:
        import win32print
        return win32print.GetDefaultPrinter()
    except Exception:      # noqa: BLE001
        return ""


class _GdiRenderer(LabelRenderer):
    """Renderer GDI : dessine sur un DC imprimante (unites = points device)."""

    def __init__(self, dc, dpi_x, dpi_y, family="Arial"):
        self.dc = dc
        self.dx = dpi_x
        self.dy = dpi_y
        self.family = family
        self._fonts = {}
        import win32ui
        self._win32ui = win32ui

    def _mm_x(self, mm):
        return int(round(mm / 25.4 * self.dx))

    def _mm_y(self, mm):
        return int(round(mm / 25.4 * self.dy))

    def _font_for(self, h_mm, bold):
        key = (round(h_mm, 2), bold)
        f = self._fonts.get(key)
        if f is None:
            # NB : pas d'option 'strike out' ici (cle LOGFONT capricieuse selon
            # les pilotes) ; le barre est trace a la main dans text().
            f = self._win32ui.CreateFont({
                "name": self.family,
                "height": -self._mm_y(h_mm),
                "weight": 700 if bold else 400,
            })
            self._fonts[key] = f
        return f

    def text(self, x, y, s, h_mm, bold=False, anchor="nw", strike=False):
        self.dc.SelectObject(self._font_for(h_mm, bold))
        tw, th = self.dc.GetTextExtent(str(s))
        px, py = self._mm_x(x), self._mm_y(y)
        # ajustement horizontal
        if anchor in ("n", "s", "center"):
            px -= tw // 2
        elif anchor in ("ne", "e", "se"):
            px -= tw
        # ajustement vertical
        if anchor in ("center", "w", "e"):
            py -= th // 2
        elif anchor in ("sw", "s", "se"):
            py -= th
        self.dc.TextOut(px, py, str(s))
        if strike and tw > 0:
            ly = py + th // 2
            thick = max(1, th // 14)
            self.dc.FillSolidRect((px, ly, px + tw, ly + thick), 0)

    def measure(self, s, h_mm, bold=False):
        self.dc.SelectObject(self._font_for(h_mm, bold))
        tw, th = self.dc.GetTextExtent(str(s))
        return tw / self.dx * 25.4, th / self.dy * 25.4

    def rect(self, x, y, w, h):
        left, top = self._mm_x(x), self._mm_y(y)
        right, bottom = self._mm_x(x + w), self._mm_y(y + h)
        if right <= left:
            right = left + 1
        self.dc.FillSolidRect((left, top, right, bottom), 0)   # 0 = noir


# Champs DEVMODE (constantes Windows, valeurs fixes).
_DM_ORIENTATION = 0x1
_DM_PAPERSIZE = 0x2
_DM_PAPERLENGTH = 0x4
_DM_PAPERWIDTH = 0x8
_DMPAPER_USER = 256
_DMORIENT_PORTRAIT = 1


def _open_label_dc(printer_name, model):
    """Ouvre un DC imprimante en fixant la taille exacte de l'etiquette.

    Regle le format papier a ``model.width_mm`` x ``model.height_mm`` (en
    dixiemes de mm) via le DEVMODE, pour que l'etiquette tombe juste sur une
    imprimante thermique (ex Xprinter XP-427D, 203 dpi, papier 20x40). En cas
    d'echec, on retombe sur le DEVMODE par defaut du pilote.
    """
    import win32ui
    name = printer_name or default_printer()
    try:
        import win32print
        import win32gui
        h = win32print.OpenPrinter(name)
        try:
            dm = win32print.GetPrinter(h, 2)["pDevMode"]
        finally:
            win32print.ClosePrinter(h)
        if dm is not None:
            dm.PaperSize = _DMPAPER_USER
            dm.PaperWidth = int(round(model.width_mm * 10))    # 0.1 mm
            dm.PaperLength = int(round(model.height_mm * 10))
            dm.Orientation = _DMORIENT_PORTRAIT
            dm.Fields |= (_DM_PAPERSIZE | _DM_PAPERWIDTH |
                          _DM_PAPERLENGTH | _DM_ORIENTATION)
            hdc = win32gui.CreateDC("WINSPOOL", name, dm)
            return win32ui.CreateDCFromHandle(hdc)
    except Exception:      # noqa: BLE001
        pass               # repli : format papier configure dans le pilote
    dc = win32ui.CreateDC()
    dc.CreatePrinterDC(name)
    return dc


def print_labels(printer_name, model, items, copies=1):
    """Imprime les etiquettes directement sur ``printer_name``.

    ``model`` : LabelModel ; ``items`` : liste de LabelItem ; ``copies`` :
    nombre d'exemplaires par article. Leve RuntimeError si l'impression n'est
    pas disponible (hors Windows) ou en cas d'echec GDI.
    """
    if not printing_available():
        raise RuntimeError(
            "L'impression directe necessite Windows avec pywin32 installe "
            "(py -m pip install pywin32).")
    import win32con

    copies = max(1, int(copies))
    try:
        dc = _open_label_dc(printer_name, model)
    except Exception as exc:      # noqa: BLE001
        raise RuntimeError("Imprimante inaccessible : %s" % exc) from exc

    dpi_x = dc.GetDeviceCaps(win32con.LOGPIXELSX)
    dpi_y = dc.GetDeviceCaps(win32con.LOGPIXELSY)
    r = _GdiRenderer(dc, dpi_x, dpi_y)
    try:
        dc.StartDoc("Etiquettes PRIME")
        for item in items:
            for _ in range(copies):
                dc.StartPage()
                layout_label(model, item, r)
                dc.EndPage()
        dc.EndDoc()
    except Exception as exc:       # noqa: BLE001
        try:
            dc.AbortDoc()
        except Exception:          # noqa: BLE001
            pass
        raise RuntimeError("Echec d'impression : %s" % exc) from exc
    finally:
        dc.DeleteDC()
