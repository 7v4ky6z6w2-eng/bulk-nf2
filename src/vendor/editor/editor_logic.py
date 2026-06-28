# -*- coding: utf-8 -*-
"""
editor_logic.py
===============

Logique *pure* (sans base de donnees ni interface) de l'editeur en masse
d'articles. Tout ce qui est ici est testable unitairement et sert a la fois
a l'interface graphique et aux tests automatiques.

On y trouve :

  * la normalisation de texte (accents/casse) ;
  * la conversion robuste de nombres saisis a la francaise ("12,50") ;
  * les calculs HT <-> TTC a partir du taux de TVA ;
  * les operations de prix en masse (fixer, +/- %, +/- montant, arrondi) ;
  * les operations sur la reference / le code-barres (chercher-remplacer,
    prefixe/suffixe, recopie de la reference) ;
  * la validation de longueur des champs texte (en octets, comme Firebird).

Les noms de colonnes de la base PRIME (table ARTICLE) sont regroupes dans
:class:`Cols` pour eviter les chaines magiques disseminees dans le code.
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass


# --------------------------------------------------------------------------- #
#  Colonnes de la table ARTICLE (base PRIME / Firebird)
# --------------------------------------------------------------------------- #
class Cols:
    """Noms des colonnes manipulees, tels que dans la base PRIME."""

    REF = "REF_ART"            # reference article = code scanne (cle naturelle)
    DESIGNATION = "DESIGNATION"
    CODE_BARRES = "CODE_BARRES"   # VARCHAR(60)
    CODE_BARRE = "CODE_BARRE"     # VARCHAR(35)
    PV_HT = "PRIXVENTEHT"
    PV_TTC = "PRIXVENTETTC"
    PA_HT = "PRIXACHATHT"
    PA_TTC = "PRIXACHATTTC"
    TVA = "TAUX_TVA"
    FAMILLE = "CODEFAMILLE"
    QTE_CARTON = "QTEPCARTON"   # quantite par carton (numerique)

    #: Colonnes affichees dans la grille, dans l'ordre.
    DISPLAY = [REF, DESIGNATION, CODE_BARRES, CODE_BARRE,
               PV_HT, PV_TTC, TVA, PA_HT, QTE_CARTON, FAMILLE]

    #: Colonnes que l'utilisateur peut modifier dans l'editeur.
    #: (FAMILLE = CODEFAMILLE : affectation/creation geree avec garde-fou FK)
    EDITABLE = {REF, CODE_BARRES, CODE_BARRE, PV_HT, PV_TTC, TVA, FAMILLE,
                QTE_CARTON}

    #: Champs "prix de vente" qui doivent rester IDENTIQUES entre eux dans la
    #: base PRIME (HT et TTC contiennent le meme prix de vente saisi).
    PRICE_FIELDS = [PV_HT, PV_TTC]

    #: Table et colonnes du referentiel des familles (base PRIME).
    FAMILLE_TABLE = "FAMILLE"
    FAMILLE_CODE = "CODEFAMILLE"
    FAMILLE_NAME = "INTITULE"
    FAMILLE_PARENT = "CODEFAMILLE_M"
    FAMILLE_TVA = "TAUX_TVA"

    #: Colonnes numeriques (prix / taux / quantites).
    NUMERIC = {PV_HT, PV_TTC, PA_HT, PA_TTC, TVA, QTE_CARTON}

    #: Longueur maximale (en octets) des champs texte modifiables. Sert de
    #: garde-fou tant que la base n'a pas confirme la vraie taille via les
    #: tables systeme (voir article_db.introspect_lengths).
    DEFAULT_MAX_LEN = {
        REF: 35,
        CODE_BARRES: 60,
        CODE_BARRE: 35,
        DESIGNATION: 100,
    }

    #: Libelles francais pour l'entete des colonnes.
    LABELS = {
        REF: "Ref. Art.",
        DESIGNATION: "Designation",
        CODE_BARRES: "Code-barres (60)",
        CODE_BARRE: "Code-barre (35)",
        PV_HT: "Prix vente HT",
        PV_TTC: "Prix vente TTC",
        TVA: "TVA %",
        PA_HT: "Prix achat HT",
        QTE_CARTON: "Qte/carton",
        FAMILLE: "Famille",
    }


# --------------------------------------------------------------------------- #
#  Texte / nombres
# --------------------------------------------------------------------------- #
def norm(s) -> str:
    """minuscule, sans accents, espaces compactes (pour comparer des libelles)."""
    if s is None:
        return ""
    s = str(s).strip().lower()
    s = "".join(c for c in unicodedata.normalize("NFD", s)
                if unicodedata.category(c) != "Mn")
    return " ".join(s.split())


def parse_number(value, default=None):
    """Convertit une saisie utilisateur en float.

    Accepte la virgule decimale francaise et les espaces (separateurs de
    milliers). Renvoie ``default`` si la valeur est vide ou invalide.

    >>> parse_number("12,50")
    12.5
    >>> parse_number("1 234,00")
    1234.0
    >>> parse_number("")
    >>> parse_number("abc", 0.0)
    0.0
    """
    if value is None:
        return default
    if isinstance(value, (int, float)):
        return float(value)
    txt = str(value).strip()
    if txt == "":
        return default
    # 1 234,56 -> 1234.56 ; gere aussi "1.234,56"
    txt = txt.replace(" ", "").replace(" ", "")
    if "," in txt:
        txt = txt.replace(".", "").replace(",", ".")
    try:
        return float(txt)
    except ValueError:
        return default


def fmt_price(value) -> str:
    """Formate un prix pour l'affichage (2 decimales, '' si None)."""
    if value is None or value == "":
        return ""
    try:
        return f"{float(value):.2f}"
    except (TypeError, ValueError):
        return str(value)


def encoded_len(text, codec="cp1252") -> int:
    """Longueur en OCTETS d'une chaine une fois encodee (comme Firebird)."""
    if text is None:
        return 0
    return len(str(text).encode(codec, errors="replace"))


def fit_text(value, max_bytes, codec="cp1252"):
    """Tronque une valeur texte a ``max_bytes`` octets sans couper un
    caractere multi-octets. Renvoie (texte_ajuste, a_ete_tronque)."""
    if value is None:
        return None, False
    s = value if isinstance(value, str) else str(value)
    raw = s.encode(codec, errors="replace")
    if len(raw) <= max_bytes:
        return s, False
    return raw[:max_bytes].decode(codec, errors="ignore"), True


# --------------------------------------------------------------------------- #
#  HT <-> TTC
# --------------------------------------------------------------------------- #
def ht_to_ttc(ht, tva, ndigits=4):
    """Prix TTC a partir du HT et du taux de TVA (en %)."""
    ht = parse_number(ht)
    tva = parse_number(tva, 0.0) or 0.0
    if ht is None:
        return None
    return round(ht * (1 + tva / 100.0), ndigits)


def ttc_to_ht(ttc, tva, ndigits=4):
    """Prix HT a partir du TTC et du taux de TVA (en %)."""
    ttc = parse_number(ttc)
    tva = parse_number(tva, 0.0) or 0.0
    if ttc is None:
        return None
    return round(ttc / (1 + tva / 100.0), ndigits)


# --------------------------------------------------------------------------- #
#  Operations de prix en masse
# --------------------------------------------------------------------------- #
@dataclass
class PriceOp:
    """Decrit une operation de prix a appliquer a une selection.

    mode :
        "set"        -> remplacer par `value`
        "inc_pct"    -> augmenter de `value` %
        "dec_pct"    -> diminuer de `value` %
        "inc_amount" -> ajouter `value`
        "dec_amount" -> retrancher `value`
        "round"      -> seulement arrondir (selon `round_to`)

    round_to :
        None      -> pas d'arrondi
        "0.99"    -> finir en .99 (prix psychologique, arrondi au dessus)
        "0.95"    -> finir en .95
        "0.90"    -> finir en .90
        "0.50"    -> au 0,50 le plus proche
        "1"       -> a l'entier le plus proche
        "0.10"    -> au 0,10 le plus proche
        "0.05"    -> au 0,05 le plus proche
    """

    mode: str
    value: float = 0.0
    round_to: str | None = None
    ndigits: int = 2

    def apply(self, current):
        """Renvoie le nouveau prix (float) a partir du prix courant.

        `current` peut etre None ; dans ce cas seul le mode "set" produit une
        valeur, les autres operations renvoient None (rien a modifier).
        """
        cur = parse_number(current)
        if self.mode == "set":
            new = float(self.value)
        elif cur is None:
            return None
        elif self.mode == "inc_pct":
            new = cur * (1 + self.value / 100.0)
        elif self.mode == "dec_pct":
            new = cur * (1 - self.value / 100.0)
        elif self.mode == "inc_amount":
            new = cur + self.value
        elif self.mode == "dec_amount":
            new = cur - self.value
        elif self.mode == "round":
            new = cur
        else:
            raise ValueError(f"mode de prix inconnu : {self.mode}")

        new = apply_rounding(new, self.round_to)
        if new < 0:
            new = 0.0
        return round(new, self.ndigits)


def apply_rounding(value, round_to):
    """Applique une regle d'arrondi commerciale a un prix."""
    if value is None or round_to in (None, "", "none"):
        return value
    import math

    if round_to in ("0.99", "0.95", "0.90"):
        cents = float(round_to)              # ex 0.99
        candidate = math.floor(value) + cents  # ex 12.40 -> 12.99
        # ne jamais descendre sous la valeur : si le palier .99 de l'euro
        # courant est sous le prix, passer a l'euro suivant (12.995 -> 13.99).
        if candidate < value - 1e-9:
            candidate += 1
        return round(candidate, 2)
    step = {"0.50": 0.5, "1": 1.0, "0.10": 0.1, "0.05": 0.05}.get(round_to)
    if step:
        return round(round(value / step) * step, 2)
    return value


# --------------------------------------------------------------------------- #
#  Operations texte (reference / code-barres)
# --------------------------------------------------------------------------- #
@dataclass
class TextOp:
    """Operation texte a appliquer a une colonne (ref ou code-barres).

    mode :
        "set"          -> remplacer par `value`
        "clear"        -> vider (None)
        "prefix"       -> ajouter `value` au debut
        "suffix"       -> ajouter `value` a la fin
        "replace"      -> remplacer `find` par `value` (sous-chaine)
        "copy_from"    -> recopier la valeur d'une autre colonne (source_value)
    """

    mode: str
    value: str = ""
    find: str = ""
    case_sensitive: bool = True

    def apply(self, current, source_value=None):
        cur = "" if current is None else str(current)
        if self.mode == "set":
            return self.value or None
        if self.mode == "clear":
            return None
        if self.mode == "prefix":
            return (self.value + cur) or None
        if self.mode == "suffix":
            return (cur + self.value) or None
        if self.mode == "copy_from":
            return (source_value if source_value not in (None, "") else None)
        if self.mode == "replace":
            if not self.find:
                return cur or None
            if self.case_sensitive:
                return cur.replace(self.find, self.value) or None
            return _ireplace(cur, self.find, self.value) or None
        raise ValueError(f"mode texte inconnu : {self.mode}")


def _ireplace(text, find, repl):
    """Remplacement insensible a la casse."""
    out, low, lf = [], text.lower(), find.lower()
    i = 0
    while True:
        j = low.find(lf, i)
        if j < 0:
            out.append(text[i:])
            break
        out.append(text[i:j])
        out.append(repl)
        i = j + len(find)
    return "".join(out)


# --------------------------------------------------------------------------- #
#  Auto-detection des colonnes (si la base n'a pas exactement ces noms)
# --------------------------------------------------------------------------- #
#: Alias possibles -> nom logique. Le premier nom de colonne reel qui
#: correspond (apres normalisation) est retenu. Permet de s'adapter a une
#: variante de schema sans rien coder en dur.
COLUMN_ALIASES = {
    Cols.REF: ["ref_art", "reference", "ref", "code_article", "codearticle"],
    Cols.DESIGNATION: ["designation", "libelle", "intitule", "designation_1"],
    Cols.CODE_BARRES: ["code_barres", "codebarres", "code_barre_s"],
    Cols.CODE_BARRE: ["code_barre", "codebarre", "ean", "ean13", "gencode"],
    Cols.PV_HT: ["prixventeht", "pv_ht", "prix_vente_ht", "pvht"],
    Cols.PV_TTC: ["prixventettc", "pv_ttc", "prix_vente_ttc", "pvttc"],
    Cols.PA_HT: ["prixachatht", "pa_ht", "prix_achat_ht", "paht"],
    Cols.PA_TTC: ["prixachatttc", "pa_ttc", "prix_achat_ttc"],
    Cols.TVA: ["taux_tva", "tva", "taux_de_tva", "txtva"],
    Cols.FAMILLE: ["codefamille", "famille", "code_famille", "rayon"],
    Cols.QTE_CARTON: ["qtepcarton", "qte_p_carton", "qte_carton",
                      "qtecarton", "qte_par_carton", "pcb"],
}


def detect_columns(available_columns):
    """Associe chaque colonne logique a la vraie colonne de la base.

    `available_columns` : liste des noms de colonnes reels (str).
    Retourne un dict {nom_logique: nom_reel}. Une colonne logique absente de
    la base n'apparait pas dans le resultat.
    """
    by_norm = {}
    for real in available_columns:
        by_norm.setdefault(norm(real).replace(" ", "_"), real)
        by_norm.setdefault(norm(real).replace(" ", ""), real)

    mapping = {}
    for logical, aliases in COLUMN_ALIASES.items():
        # priorite : nom exact
        for real in available_columns:
            if norm(real).replace(" ", "_") == norm(logical).replace(" ", "_"):
                mapping[logical] = real
                break
        if logical in mapping:
            continue
        for alias in aliases:
            key = norm(alias).replace(" ", "_")
            if key in by_norm:
                mapping[logical] = by_norm[key]
                break
            key2 = norm(alias).replace(" ", "")
            if key2 in by_norm:
                mapping[logical] = by_norm[key2]
                break
    return mapping
