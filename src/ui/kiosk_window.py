"""Fenêtre principale du kiosque prix (plein écran, Tkinter).

Réécrit en Tkinter (au lieu de Qt/PySide2) pour fonctionner sur du matériel
32 bits ancien (Intel Atom, Windows 10 32 bits) où la couche de liaison C++
de Qt (shiboken2) refuse de se charger. Tkinter est fourni avec Python : aucune
DLL Qt, aucun runtime Visual C++ supplémentaire, aucun shiboken2.

Fonctionnement :
  - Écran de repos  : logo Prime Office + invite bilingue FR/AR, halo animé.
  - Après scan      : nom de l'article + PRIX géant.
  - Retour au repos : automatique après cfg.ui.idle_reset_seconds secondes.
  - Quitter         : raccourci cfg.ui.exit_hotkey (par défaut Ctrl+Alt+Q).
  - Réglages        : raccourci Ctrl+Alt+S pour rouvrir l'assistant.
"""

from __future__ import annotations

import math
import os
import queue
import sys
import threading
import uuid
from typing import Optional

import tkinter as tk
import tkinter.font as tkfont

try:
    from PIL import Image, ImageTk
    _HAS_PIL = True
except Exception:  # noqa: BLE001 — Pillow absent : on affiche sans photo
    _HAS_PIL = False

import i18n
import config as _cfgmod
from config import AppConfig
from database import Database
from woocommerce import WooClient

# ---------------------------------------------------------------------------
# Palette Prime Office (thème sombre)
# ---------------------------------------------------------------------------

_C_BG        = "#0A0618"   # fond quasi-noir
_C_BG_TOP    = "#221058"   # haut du dégradé (violet profond)
_C_BG2       = "#170D38"   # violet foncé
_C_TEXT      = "#F1EDFB"   # texte principal
_C_MUTED     = "#8A80B4"   # texte discret
_C_LIME      = "#C6F432"   # accent — prix
_C_LIME_DARK = "#9CCB14"   # lime foncé
_C_VIOLET    = "#5B2EE5"   # violet accent
_C_LINE      = "#3A2A6E"   # séparateurs
_C_SURFACE   = "#1A0F40"   # cadre / surface (zone photo)
_C_DANGER    = "#FF3355"   # erreur / introuvable
_C_LOGO_INK  = "#160B2E"   # encre du logo (sur badge lime)

_FONT_FAMILY = "Segoe UI"


# ---------------------------------------------------------------------------
# Utilitaires couleur / dessin
# ---------------------------------------------------------------------------

def _lerp(c1: str, c2: str, t: float) -> str:
    """Interpole deux couleurs hexadécimales (#rrggbb)."""
    t = max(0.0, min(1.0, t))
    r1, g1, b1 = int(c1[1:3], 16), int(c1[3:5], 16), int(c1[5:7], 16)
    r2, g2, b2 = int(c2[1:3], 16), int(c2[3:5], 16), int(c2[5:7], 16)
    r = int(r1 + (r2 - r1) * t)
    g = int(g1 + (g2 - g1) * t)
    b = int(b1 + (b2 - b1) * t)
    return f"#{r:02x}{g:02x}{b:02x}"


def _round_rect_points(x1, y1, x2, y2, r):
    """Points d'un rectangle à coins arrondis (pour create_polygon smooth)."""
    return [
        x1 + r, y1, x2 - r, y1, x2, y1, x2, y1 + r,
        x2, y2 - r, x2, y2, x2 - r, y2, x1 + r, y2,
        x1, y2, x1, y2 - r, x1, y1 + r, x1, y1,
    ]


def _hex_rgb(c: str):
    return (int(c[1:3], 16), int(c[3:5], 16), int(c[5:7], 16))


def _format_dinars(v: float) -> str:
    """Montant arrondi au dinar entier : 999.90 -> « 1 000 DA »."""
    n = int(round(v))
    return f"{n:,}".replace(",", " ") + f" {i18n.CURRENCY}"


def _find_asset(name: str) -> Optional[str]:
    """Localise un fichier d'assets en dev comme une fois packagé (PyInstaller)."""
    candidates = []
    base = getattr(sys, "_MEIPASS", None)          # onefile
    if base:
        candidates.append(os.path.join(base, "assets", name))
    candidates.append(os.path.join(_cfgmod.app_dir(), "assets", name))  # onedir
    candidates.append(os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "assets", name))
    for c in candidates:
        if c and os.path.exists(c):
            return c
    return None


# ---------------------------------------------------------------------------
# Fenêtre kiosque
# ---------------------------------------------------------------------------

class KioskWindow:
    """Kiosque plein écran de vérification de prix (Tkinter)."""

    def __init__(self, root: tk.Tk, cfg: AppConfig, db: Database, woo: WooClient):
        self.root = root
        self._cfg = cfg
        self._db = db      # conservé pour compat ; les recherches ouvrent leur
        self._woo = woo    # propre connexion (sécurité des threads).

        self._state = "idle"          # "idle" | "result"
        self._result: dict = {}
        self._current_scan_id: Optional[str] = None
        self._idle_after: Optional[str] = None
        self._phase = 0.0
        self._glow_ids: list = []
        self._rendered_once = False
        self._alive = True
        self._queue: "queue.Queue" = queue.Queue()
        self._photo = None            # ImageTk.PhotoImage courant
        self._photo_state = "none"    # "none" | "loading" | "ok"

        # Logo Prime Office (PNG rendu depuis le SVG). Repli « PO » si absent.
        self._logo_src = None
        self._badge_cache: dict = {}
        if _HAS_PIL:
            lp = _find_asset("logo.png")
            if lp:
                try:
                    self._logo_src = Image.open(lp).convert("RGBA")
                except Exception:  # noqa: BLE001
                    self._logo_src = None

        # Capturer les erreurs de rappel Tkinter (sinon avalées en mode
        # --windowed : fenêtre qui reste noire sans message d'erreur).
        root.report_callback_exception = self._report_exc

        # --- Fenêtre ---------------------------------------------------------
        # On mappe d'abord une fenêtre normale (taille écran) — le mode plein
        # écran n'est appliqué qu'ENSUITE, une fois la fenêtre affichée. Sur du
        # vieux matériel 32 bits (Intel), régler « -fullscreen » avant le
        # premier affichage peut produire une fenêtre invisible en arrière-plan.
        root.title("Affichage Prix Netfact")
        root.configure(bg=_C_BG)
        sw = root.winfo_screenwidth()
        sh = root.winfo_screenheight()
        if cfg.ui.fullscreen:
            root.geometry(f"{sw}x{sh}+0+0")
        else:
            root.geometry("1024x720")

        self.canvas = tk.Canvas(root, bg=_C_BG, highlightthickness=0, bd=0)
        self.canvas.pack(fill="both", expand=True)

        # Champ invisible qui capte les frappes du lecteur code-barres.
        self._entry = tk.Entry(root, width=4)
        self._entry.place(x=-500, y=-500, width=10, height=10)
        self._entry.bind("<Return>", self._on_scan)
        self._entry.bind("<FocusOut>", lambda e: self.root.after(50, self._refocus))
        self._entry.focus_set()

        # Garder le focus sur le champ code-barres.
        self.canvas.bind("<Button-1>", lambda e: self._refocus())
        root.bind("<Button-1>", lambda e: self._refocus())

        # Redessiner lors du redimensionnement / affichage.
        self.canvas.bind("<Configure>", lambda e: self._render())
        self.canvas.bind("<Map>", lambda e: self._render())

        # Raccourcis clavier.
        self._bind_hotkey(cfg.ui.exit_hotkey, self._on_quit)
        self._bind_hotkey("Ctrl+Alt+S", self._open_setup)
        self._bind_hotkey("Ctrl+Alt+D", self._dump_schema)   # diagnostic schéma

        # Forcer l'affichage au premier plan (kiosque).
        root.deiconify()
        root.update_idletasks()
        root.lift()
        try:
            root.attributes("-topmost", True)
        except tk.TclError:
            pass
        root.focus_force()
        _cfgmod.log(f"Fenêtre affichée — mapped={root.winfo_ismapped()} "
                    f"geo={root.winfo_geometry()} écran={sw}x{sh}")

        # Passer en plein écran APRÈS le premier affichage.
        if cfg.ui.fullscreen:
            self.root.after(300, self._go_fullscreen)

        # Boucles.
        self.root.after(40, self._tick)
        self.root.after(30, self._poll_queue)
        # Rendu initial + rendu différé (au cas où la taille n'est pas encore
        # connue au moment de la construction).
        self._render()
        self.root.after(120, self._render)
        self.root.after(500, self._render)
        _cfgmod.log("KioskWindow construite")

    def _go_fullscreen(self) -> None:
        try:
            self.root.attributes("-fullscreen", True)
        except tk.TclError:
            try:
                self.root.state("zoomed")
            except tk.TclError:
                pass
        self.root.lift()
        self.root.focus_force()
        _cfgmod.log(f"Plein écran — mapped={self.root.winfo_ismapped()} "
                    f"geo={self.root.winfo_geometry()}")
        self._render()

    def _report_exc(self, exc, val, tb):
        import traceback
        _cfgmod.log("ERREUR DE RAPPEL Tkinter :\n"
                    + "".join(traceback.format_exception(exc, val, tb)))

    # --- Raccourcis --------------------------------------------------------

    def _bind_hotkey(self, spec: str, handler) -> None:
        """Convertit « Ctrl+Alt+Q » en séquence Tk et l'attache."""
        parts = [p.strip() for p in spec.split("+") if p.strip()]
        if not parts:
            return
        mods = {"ctrl": "Control", "control": "Control",
                "alt": "Alt", "shift": "Shift"}
        seq_mods = []
        key = parts[-1]
        for p in parts[:-1]:
            m = mods.get(p.lower())
            if m:
                seq_mods.append(m)
        key = key.lower() if len(key) == 1 else key
        sequence = "<" + "-".join(seq_mods + [key]) + ">"
        try:
            self.root.bind_all(sequence, lambda e: handler())
        except tk.TclError:
            pass

    # --- Focus -------------------------------------------------------------

    def _refocus(self) -> None:
        try:
            self._entry.focus_set()
        except tk.TclError:
            pass

    # --- Scan code-barres --------------------------------------------------

    def _on_scan(self, event=None) -> None:
        code = self._entry.get().strip()
        self._entry.delete(0, "end")
        if not code:
            return

        if self._idle_after is not None:
            self.root.after_cancel(self._idle_after)
            self._idle_after = None

        scan_id = str(uuid.uuid4())
        self._current_scan_id = scan_id

        cfg_fb = self._cfg.firebird
        t = threading.Thread(
            target=self._lookup_worker, args=(cfg_fb, code, scan_id), daemon=True
        )
        t.start()

    def _lookup_worker(self, cfg_fb, code: str, scan_id: str) -> None:
        """Exécuté dans un thread : connexion Firebird dédiée puis recherche."""
        db = Database(cfg_fb)
        try:
            article = db.lookup_article(code)
            self._queue.put(("lookup", scan_id, article, None))
        except Exception as exc:  # noqa: BLE001 — remonté à l'UI
            self._queue.put(("lookup", scan_id, None, exc))
        finally:
            db.close()

    def _start_image_fetch(self, ref_art: str, scan_id: str) -> None:
        """Récupère la photo WooCommerce (par SKU) dans un thread."""
        woo = self._woo
        if woo is None or not getattr(woo, "configured", False):
            return

        def _work():
            try:
                path = woo.get_image(ref_art)
            except Exception:  # noqa: BLE001 — jamais bruyant
                path = None
            self._queue.put(("image", scan_id, path))

        threading.Thread(target=_work, daemon=True).start()

    def _poll_queue(self) -> None:
        if not self._alive:
            return
        try:
            while True:
                msg = self._queue.get_nowait()
                kind = msg[0]
                if kind in ("schema", "schema_err"):
                    from tkinter import messagebox
                    if kind == "schema":
                        messagebox.showinfo("Diagnostic schéma",
                                            f"Fichier créé :\n{msg[1]}")
                    else:
                        messagebox.showerror("Diagnostic schéma", msg[1])
                    self._refocus()
                    continue
                scan_id = msg[1]
                if scan_id != self._current_scan_id:
                    continue
                if kind == "lookup":
                    _, _, article, error = msg
                    if error is not None:
                        self._show_error(i18n.DB_ERROR_FR, i18n.DB_ERROR_AR)
                    elif article is None:
                        self._show_error(i18n.NOT_FOUND_FR, i18n.NOT_FOUND_AR)
                    else:
                        self._show_article(article)
                elif kind == "image":
                    _, _, path = msg
                    self._load_photo(path)
        except queue.Empty:
            pass
        self.root.after(30, self._poll_queue)

    def _load_photo(self, path: Optional[str]) -> None:
        """Charge la photo téléchargée et déclenche un nouveau rendu."""
        if not path or not _HAS_PIL:
            self._photo = None
            self._photo_state = "none"
            self._render()
            return
        try:
            w = self.canvas.winfo_width() or self.root.winfo_screenwidth()
            h = self.canvas.winfo_height() or self.root.winfo_screenheight()
            side = max(120, int(min(w * 0.34, h * 0.58)))
            im = Image.open(path).convert("RGB")
            resample = getattr(getattr(Image, "Resampling", Image), "LANCZOS", 1)
            im.thumbnail((side, side), resample)
            self._photo = ImageTk.PhotoImage(im)
            self._photo_state = "ok"
        except Exception:  # noqa: BLE001 — photo illisible : on continue sans
            self._photo = None
            self._photo_state = "none"
        self._render()

    # --- Transitions d'écran ----------------------------------------------

    def show_idle(self) -> None:
        if self._idle_after is not None:
            self.root.after_cancel(self._idle_after)
            self._idle_after = None
        self._current_scan_id = None
        self._state = "idle"
        self._photo = None
        self._photo_state = "none"
        self._render()
        self._refocus()

    def _arm_idle_timer(self) -> None:
        if self._idle_after is not None:
            self.root.after_cancel(self._idle_after)
        self._idle_after = self.root.after(
            max(1, self._cfg.ui.idle_reset_seconds) * 1000, self.show_idle
        )

    def _show_article(self, article) -> None:
        self._state = "result"
        self._result = {
            "designation": (article.designation or "").upper(),
            "price": i18n.format_price(article.prix_vente_ht),
            "ref": (article.ref_art or "").upper(),
            "tiers": list(getattr(article, "tiers", []) or []),
            "error": None,
        }
        # Réinitialiser la photo et lancer sa récupération.
        self._photo = None
        configured = self._woo is not None and getattr(self._woo, "configured", False)
        self._photo_state = "loading" if (configured and _HAS_PIL) else "none"
        self._render()
        if configured and _HAS_PIL:
            self._start_image_fetch(article.ref_art, self._current_scan_id)
        self._arm_idle_timer()
        self._refocus()

    def _show_error(self, fr: str, ar: str) -> None:
        self._state = "result"
        self._result = {"error": (fr, ar)}
        self._photo = None
        self._photo_state = "none"
        self._render()
        self._arm_idle_timer()
        self._refocus()

    # --- Animation du halo (repos uniquement) ------------------------------

    def _tick(self) -> None:
        if not self._alive:
            return
        self._phase += 0.05
        if self._state == "idle" and self._glow_ids:
            pulse = 0.5 + 0.5 * math.sin(self._phase)
            for item_id, base in self._glow_ids:
                col = _lerp(_C_BG2, _C_LIME, base * (0.35 + 0.65 * pulse))
                try:
                    self.canvas.itemconfigure(item_id, fill=col, outline=col)
                except tk.TclError:
                    pass
        self.root.after(40, self._tick)

    # --- Rendu -------------------------------------------------------------

    def _render(self) -> None:
        c = self.canvas
        c.delete("all")
        self._glow_ids = []
        w = c.winfo_width()
        h = c.winfo_height()
        if w <= 1 or h <= 1:
            # Taille pas encore connue : replier sur la taille de l'écran pour
            # dessiner quelque chose immédiatement (corrigé au <Configure>).
            w = self.root.winfo_screenwidth()
            h = self.root.winfo_screenheight()
        if w <= 1 or h <= 1:
            return
        if not self._rendered_once:
            self._rendered_once = True
            _cfgmod.log(f"Premier rendu — {w}x{h}, état={self._state}")

        self._draw_gradient(w, h)
        self._draw_brand_bar(w)

        if self._state == "idle":
            self._render_idle(w, h)
        else:
            self._render_result(w, h)

    def _draw_gradient(self, w: int, h: int) -> None:
        c = self.canvas
        # Dégradé vertical violet → fond, en bandes horizontales de 4 px.
        step = 4
        for y in range(0, h, step):
            t = y / max(1, h)
            col = _lerp(_C_BG_TOP, _C_BG, min(1.0, t * 1.15))
            c.create_rectangle(0, y, w, y + step, outline="", fill=col)

    def _draw_brand_bar(self, w: int) -> None:
        c = self.canvas
        pad = 30
        badge = 40
        self._draw_logo(pad, 16, badge)
        c.create_text(
            pad + badge + 14, 24, anchor="w", text="PRIME OFFICE",
            fill=_C_TEXT, font=(_FONT_FAMILY, -18, "bold"),
        )
        c.create_text(
            pad + badge + 14, 46, anchor="w", text="ORAN · ALGÉRIE",
            fill=_C_LIME, font=(_FONT_FAMILY, -11),
        )
        c.create_text(
            w - pad, 32, anchor="e", text="VÉRIFICATEUR DE PRIX",
            fill=_C_MUTED, font=(_FONT_FAMILY, -12),
        )
        c.create_line(0, 72, w, 72, fill=_C_LIME, width=2)

    def _make_badge(self, size: int):
        """Construit un badge lime arrondi avec le vrai logo composité (Pillow)."""
        try:
            from PIL import ImageDraw
            img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
            # Dégradé lime vertical.
            top, bot = _hex_rgb(_C_LIME), _hex_rgb(_C_LIME_DARK)
            grad = Image.new("RGB", (1, size))
            for yy in range(size):
                t = yy / max(1, size - 1)
                grad.putpixel((0, yy),
                              tuple(int(top[i] + (bot[i] - top[i]) * t) for i in range(3)))
            grad = grad.resize((size, size))
            # Masque arrondi.
            mask = Image.new("L", (size, size), 0)
            ImageDraw.Draw(mask).rounded_rectangle(
                [0, 0, size - 1, size - 1], radius=int(size * 0.24), fill=255)
            img.paste(grad, (0, 0), mask)
            # Logo centré avec marge.
            if self._logo_src is not None:
                pad = int(size * 0.17)
                inner = max(1, size - 2 * pad)
                lw, lh = self._logo_src.size
                scale = min(inner / lw, inner / lh)
                nw, nh = max(1, int(lw * scale)), max(1, int(lh * scale))
                resample = getattr(getattr(Image, "Resampling", Image), "LANCZOS", 1)
                logo = self._logo_src.resize((nw, nh), resample)
                img.paste(logo, ((size - nw) // 2, (size - nh) // 2), logo)
            return ImageTk.PhotoImage(img)
        except Exception:  # noqa: BLE001
            return None

    def _draw_logo(self, x: int, y: int, size: int) -> None:
        """Badge logo Prime Office (image) ou repli « PO »."""
        c = self.canvas
        size = int(size)
        if _HAS_PIL and self._logo_src is not None:
            photo = self._badge_cache.get(size)
            if photo is None:
                photo = self._make_badge(size)
                self._badge_cache[size] = photo
            if photo is not None:
                c.create_image(x + size / 2, y + size / 2, image=photo)
                return
        # Repli : carré lime arrondi + « PO ».
        r = size * 0.24
        pts = _round_rect_points(x, y, x + size, y + size, r)
        c.create_polygon(pts, smooth=True, fill=_C_LIME, outline=_C_LIME_DARK)
        c.create_text(
            x + size / 2, y + size / 2, text="PO",
            fill=_C_LOGO_INK, font=(_FONT_FAMILY, -int(size * 0.44), "bold"),
        )

    def _render_idle(self, w: int, h: int) -> None:
        c = self.canvas
        cx = w / 2
        cy_logo = h * 0.40
        big = int(min(w, h) * 0.22)

        # Halo animé : anneaux concentriques derrière le badge.
        n = 8
        for i in range(n):
            frac = (n - i) / n            # 1.0 (grand) → petit
            rad = big * (0.62 + frac * 0.9)
            base = (i + 1) / n            # intérieur plus lumineux
            oid = c.create_oval(
                cx - rad, cy_logo - rad, cx + rad, cy_logo + rad,
                fill=_C_BG2, outline=_C_BG2,
            )
            self._glow_ids.append((oid, base))

        # Badge logo (au-dessus du halo).
        self._draw_logo(int(cx - big / 2), int(cy_logo - big / 2), big)

        # Invites.
        y = h * 0.66
        c.create_text(
            cx, y, text=i18n.SCAN_PROMPT_FR, fill=_C_TEXT,
            font=(_FONT_FAMILY, -38, "normal"), width=int(w * 0.8), justify="center",
        )
        c.create_text(
            cx, y + 70, text=i18n.SCAN_PROMPT_AR, fill=_C_TEXT,
            font=(_FONT_FAMILY, -42, "normal"), width=int(w * 0.8), justify="center",
        )
        c.create_text(
            cx, y + 140, text="━━  SCANNER UN ARTICLE  ━━", fill=_C_MUTED,
            font=(_FONT_FAMILY, -14),
        )

    def _render_result(self, w: int, h: int) -> None:
        c = self.canvas
        cx = w / 2
        cy = h * 0.52

        error = self._result.get("error")
        if error:
            fr, ar = error
            c.create_text(
                cx, cy - 40, text=fr, fill=_C_DANGER,
                font=(_FONT_FAMILY, -52, "bold"),
                width=int(w * 0.85), justify="center",
            )
            c.create_text(
                cx, cy + 40, text=ar, fill=_C_DANGER,
                font=(_FONT_FAMILY, -52, "normal"),
                width=int(w * 0.85), justify="center",
            )
            return

        # Deux dispositions : avec photo (deux colonnes) ou centrée.
        if self._photo is not None or self._photo_state != "none":
            self._render_result_photo(w, h)
        else:
            self._render_result_centered(w, h)

    def _render_result_centered(self, w: int, h: int) -> None:
        c = self.canvas
        cx = w / 2
        tiers = self._result.get("tiers") or []
        # On remonte le bloc si des offres quantité doivent s'afficher.
        y_desig = 0.24 if tiers else 0.30
        y_div = 0.335 if tiers else 0.40
        y_price = 0.52 if tiers else 0.60
        y_ref = 0.60 if tiers else 0.72

        c.create_text(
            cx, h * y_desig, text=self._result["designation"], fill=_C_TEXT,
            font=(_FONT_FAMILY, -40, "bold"),
            width=int(w * 0.85), justify="center",
        )
        c.create_line(cx - w * 0.18, h * y_div, cx + w * 0.18, h * y_div,
                      fill=_C_LINE, width=1)
        self._draw_price(cx, h * y_price, int(min(w * 0.18, h * 0.24)),
                         max_width=w * 0.85)
        ref = self._result.get("ref")
        if ref:
            c.create_text(cx, h * y_ref, text=f"RÉF : {ref}", fill=_C_MUTED,
                          font=(_FONT_FAMILY, -16))
        if tiers:
            self._draw_tiers(cx, h * 0.68, heading_size=24, line_size=46)

    def _render_result_photo(self, w: int, h: int) -> None:
        c = self.canvas
        # ── Colonne photo (gauche) ──────────────────────────────────────────
        photo_cx = w * 0.30
        photo_cy = h * 0.55
        side = max(120, int(min(w * 0.34, h * 0.58)))
        x1, y1 = photo_cx - side / 2, photo_cy - side / 2
        x2, y2 = photo_cx + side / 2, photo_cy + side / 2
        pts = _round_rect_points(x1, y1, x2, y2, side * 0.06)
        c.create_polygon(pts, smooth=True, fill=_C_SURFACE, outline=_C_LINE)
        if self._photo is not None:
            c.create_image(photo_cx, photo_cy, image=self._photo)
        else:
            msg = ("Chargement…" if self._photo_state == "loading"
                   else "Photo indisponible")
            c.create_text(photo_cx, photo_cy, text=msg, fill=_C_MUTED,
                          font=(_FONT_FAMILY, -18))

        # ── Colonne texte (droite) ──────────────────────────────────────────
        tcx = w * 0.66
        tiers = self._result.get("tiers") or []
        y_desig = 0.26 if tiers else 0.32
        y_div = 0.37 if tiers else 0.43
        y_price = 0.55 if tiers else 0.62
        y_ref = 0.63 if tiers else 0.72

        c.create_text(
            tcx, h * y_desig, text=self._result["designation"], fill=_C_TEXT,
            font=(_FONT_FAMILY, -34, "bold"),
            width=int(w * 0.52), justify="center",
        )
        c.create_line(tcx - w * 0.14, h * y_div, tcx + w * 0.14, h * y_div,
                      fill=_C_LINE, width=1)
        self._draw_price(tcx, h * y_price, int(min(w * 0.13, h * 0.22)),
                         max_width=w * 0.52)
        ref = self._result.get("ref")
        if ref:
            c.create_text(tcx, h * y_ref, text=f"RÉF : {ref}", fill=_C_MUTED,
                          font=(_FONT_FAMILY, -15))
        if tiers:
            self._draw_tiers(tcx, h * 0.70, heading_size=19, line_size=34,
                             max_width=w * 0.52, max_lines=2)

    def _draw_tiers(self, center_x: float, y0: float, heading_size: int = 20,
                    line_size: int = 30, max_width: Optional[float] = None,
                    max_lines: int = 3) -> None:
        """Affiche les offres quantité : « qté × prix unitaire = total »."""
        tiers = self._result.get("tiers") or []
        if not tiers:
            return
        c = self.canvas
        c.create_text(center_x, y0, text="OFFRE QUANTITÉ", fill=_C_LIME,
                      font=(_FONT_FAMILY, -heading_size, "bold"))
        y = y0 + int(heading_size * 2.1)
        for (qmin, qmax, prix) in tiers[:max_lines]:
            total = qmin * prix
            qn = int(qmin) if float(qmin).is_integer() else qmin
            unit = i18n.format_price(prix).rsplit(" ", 1)[0]   # sans « DA »
            line = f"{qn} × {unit} = {_format_dinars(total)}"
            c.create_text(center_x, y, text=line, fill=_C_LIME,
                          font=(_FONT_FAMILY, -line_size, "bold"))
            y += int(line_size * 1.4)

    def _draw_price(self, center_x: float, baseline_y: float,
                    num_size: int, max_width: Optional[float] = None) -> None:
        """Dessine « nombre » (lime) + « DA », en réduisant pour tenir dans
        max_width si nécessaire."""
        price = self._result["price"]
        parts = price.rsplit(" ", 1)
        num = parts[0]
        cur = parts[1] if len(parts) > 1 else i18n.CURRENCY
        while True:
            cur_size = max(12, int(num_size * 0.34))
            f_num = tkfont.Font(family=_FONT_FAMILY, size=-num_size, weight="bold")
            f_cur = tkfont.Font(family=_FONT_FAMILY, size=-cur_size, weight="bold")
            wn = f_num.measure(num)
            wc = f_cur.measure(cur)
            gap = int(num_size * 0.12)
            total = wn + gap + wc
            if max_width is None or total <= max_width or num_size <= 24:
                break
            num_size = int(num_size * 0.9)
        start_x = center_x - total / 2
        self.canvas.create_text(start_x, baseline_y, anchor="sw", text=num,
                                fill=_C_LIME, font=f_num)
        self.canvas.create_text(start_x + wn + gap, baseline_y, anchor="sw",
                                text=cur, fill=_C_LIME_DARK, font=f_cur)

    # --- Réglages / quitter ------------------------------------------------

    def _open_setup(self) -> None:
        from ui.setup_window import SetupDialog
        from config import load
        dlg = SetupDialog(self.root, load())
        if dlg.result:
            self._cfg = load()
        self._refocus()

    def _dump_schema(self) -> None:
        """Ctrl+Alt+D : écrit schema.txt à côté de l'exe (thread DB dédié)."""
        cfg_fb = self._cfg.firebird

        def work():
            try:
                db = Database(cfg_fb)
                try:
                    text = db.dump_schema()
                finally:
                    db.close()
                path = os.path.join(_cfgmod.app_dir(), "schema.txt")
                with open(path, "w", encoding="utf-8") as fh:
                    fh.write(text)
                self._queue.put(("schema", path))
            except Exception as exc:  # noqa: BLE001
                self._queue.put(("schema_err", str(exc)))

        threading.Thread(target=work, daemon=True).start()

    def _on_quit(self) -> None:
        self._alive = False
        try:
            self.root.destroy()
        except tk.TclError:
            pass
