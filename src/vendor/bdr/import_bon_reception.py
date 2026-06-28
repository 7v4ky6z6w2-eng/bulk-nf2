#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Import d'un Bon de Reception a partir d'un fichier Excel fournisseur.

Principe
--------
Au lieu de saisir les bons de reception a la main, le fournisseur exporte
un fichier Excel (liste d'articles avec leur prix de vente). Ce script
importe ce fichier directement dans la base Firebird de la gestion
commerciale sous forme d'un *Bon de reception* (type de piece PC_AC_B) :

  * Les articles qui n'existent pas encore sont crees automatiquement.
  * Le PRIX D'ACHAT HT de l'article = le prix indique sur l'Excel du
    fournisseur (c'est le prix que vous payez l'article).
  * Le PRIX DE VENTE est laisse VIDE : vous fixerez vous-meme le prix de
    vente plus tard dans le logiciel.
  * Les lignes du bon de reception alimentent le stock (QTE) avec le meme
    prix d'achat, comme une reception saisie manuellement.

Le script reproduit exactement la logique interne du logiciel :
  - NOPIECE / NOITEM sont tires des generateurs NEXTPIECE / NEXTITEM ;
  - REF_PIECE est attribue automatiquement par le trigger INSERT_PIECE ;
  - COEFF = 1 et ANNULEE = 1 sur chaque ligne  -> le stock est bien
    incremente (verifie via la procedure SPSTOCK du logiciel) ;
  - PRIXHT de la ligne = prix d'achat  -> le PUMP (cout moyen pondere)
    est calcule correctement par le logiciel.

Usage
-----
    python import_bon_reception.py --config config.json --excel fournisseur.xlsx
    python import_bon_reception.py --config config.json --excel f.xlsx --dry-run

Dependances : openpyxl, fdb   (pip install -r requirements.txt)
"""

import argparse
import json
import os
import re
import sys
import datetime
import unicodedata

try:
    import openpyxl
except ImportError:
    sys.exit("Module manquant : openpyxl  ->  pip install openpyxl")

try:
    import fdb
except ImportError:
    sys.exit("Module manquant : fdb  ->  pip install fdb")


# --------------------------------------------------------------------------- #
#  Configuration par defaut (surchargeable par le fichier --config)
# --------------------------------------------------------------------------- #
DEFAULT_CONFIG = {
    # --- Connexion Firebird --------------------------------------------------
    "host": "localhost",            # "" ou null = acces local direct au fichier
    "port": 3050,
    "database": "C:\\\\PRIME\\\\PR22.FDB",  # chemin du fichier .FDB
    "user": "SYSDBA",
    "password": "masterkey",
    # Charset de connexion (base en charset NONE = octets bruts). Le logiciel
    # ecrit selon la page de code ANSI de Windows : WIN1256 pour l'arabe (+
    # francais accentue en minuscules), WIN1252 pour le francais seul.
    "charset": "WIN1256",

    # --- Type de piece -------------------------------------------------------
    "code_type_piece": "PC_AC_B",   # PC_AC_B = "Bons de reception"
    "etat": None,                   # etat de la piece : le logiciel laisse NULL

    # --- Tiers / depot (optionnels) -----------------------------------------
    "code_tiers": "",               # code fournisseur ; "" = aucun
    "raison_sociale": "",           # nom du fournisseur (si creation)
    "code_depot": "",               # code depot ; "" = aucun
    "create_missing_tiers": True,   # creer le fournisseur/depot s'ils manquent

    # --- Valeurs par defaut pour les nouveaux articles ----------------------
    "default_famille": "TOUS",      # famille racine / de repli (code d'une famille existante)
    "default_famille_intitule": "Tous",
    "default_unite": "",            # code unite de base ; "" = aucune (comme le logiciel)
    "default_unite_intitule": "Unite",
    "default_tva": 19,              # TVA par defaut si absente de l'Excel

    # --- Prix de vente automatique (nouveaux articles seulement) ------------
    "prix_vente_auto": True,        # calculer un prix de vente = prix achat + marge
    "marge_pct": 50,                # marge appliquee au prix d'achat (50 = +50%)
    # Arrondi VERS LE HAUT par paliers : [seuil_max, pas]. null = au-dela.
    #   < 200 -> au 5 superieur ; 200-999 -> au 10 superieur ; >= 1000 -> au 50 superieur.
    "arrondi_paliers": [[200, 5], [1000, 10], [None, 50]],
    "match_famille_par_intitule": True,  # associer la colonne "Famille" a une famille existante
    "create_missing_familles": False,    # creer une famille par son NOM si aucune ne correspond
                                         # (defaut False : on choisit une famille existante)
    "calc_prix_achat_ttc": True,    # renseigner PRIXACHATTTC (= PRIXACHATHT, identiques)

    # --- Rapprochement par designation (lignes sans code-barres) ------------
    "match_par_designation": True,  # retrouver un article existant via le numero du libelle
    "maj_prix_achat_seuil_pct": 40, # alerte si le prix achat varie de plus de X% (OCR)
    # Recopier la reference dans la designation si elle n'y figure pas (et que
    # ce n'est pas un code-barres) : utile pour les fournisseurs au libelle
    # generique ('stylo' -> 'stylo 70010').
    "ref_dans_designation": True,
    # Renseigner le code-barres (EQUIV_CBARRES, affiche dans la fiche article)
    # y compris sur un article DEJA existant qui n'en a pas encore.
    "maj_code_barres": True,

    # --- Tracabilite --------------------------------------------------------
    "refdoc": "",                   # N° du bon fournisseur (stocke dans PIECE.REFDOC)

    # --- Numerotation NOPIECE / NOITEM --------------------------------------
    # Le logiciel numerote en MAX(NOPIECE)+1 (le generateur peut etre obsolete).
    # On calcule donc le prochain numero a partir du MAX existant ET du
    # generateur, en ignorant les plages reservees (ex: 8000000 pour les
    # inventaires) au-dela de ce seuil.
    "reserved_id_threshold": 1000000,

    # --- Mapping des colonnes Excel -> alias acceptes (insensible accents/casse)
    "colonnes": {
        "ref_art":     ["ref. art.", "ref art", "reference", "ref", "code article", "code"],
        "designation": ["designation", "designation + qte", "libelle", "intitule", "article"],
        "qte":         ["qte", "quantite", "quantity", "qty"],
        "prix":        ["prix ht", "prix vente ht", "pu ht", "prix unitaire ht", "prix"],
        "tva":         ["tva", "taux tva", "taux de tva"],
        "famille":     ["famille", "rayon", "categorie"],
        "code_barres": ["code barres", "code barre", "code-barres", "codebarre",
                        "code a barre", "code a barres", "ean", "ean13", "gencode",
                        "barcode", "code barre article"],
    },
    # Code-barres : dans le logiciel, c'est la REFERENCE ARTICLE (Ref. Art.)
    # qui sert de code scanne (CODE_BARRES reste vide, et il porte un index
    # UNIQUE). On laisse donc CODE_BARRES vide par defaut : scanner le
    # code-barres retrouve l'article par sa reference, sans ressaisie.
    # Mettre True pour recopier malgre tout la reference dans CODE_BARRES,
    # ou fournir une colonne code-barres distincte dans l'Excel.
    "barcode_depuis_ref": False,
    # Colonne a utiliser comme prix d'achat (= prix de vente du fournisseur)
    "colonne_prix": "prix",
}


# --------------------------------------------------------------------------- #
#  Utilitaires
# --------------------------------------------------------------------------- #
def norm(s):
    """Normalise un libelle : minuscule, sans accents, espaces compactes."""
    if s is None:
        return ""
    s = str(s).strip().lower()
    s = "".join(c for c in unicodedata.normalize("NFD", s)
                if unicodedata.category(c) != "Mn")
    return " ".join(s.split())


def to_float(v, default=0.0):
    if v is None or v == "":
        return default
    if isinstance(v, (int, float)):
        return float(v)
    txt = str(v).strip().replace(" ", "").replace(",", ".")
    try:
        return float(txt)
    except ValueError:
        return default


def looks_like_barcode(s):
    """Vrai si 's' ressemble a un code-barres (EAN/UPC) : une suite de chiffres
    de 8 caracteres ou plus (EAN-8, UPC-12, EAN-13). Les references 'courtes'
    (ex. 70010, STY-12) ne sont PAS considerees comme des code-barres."""
    s = (s or "").strip()
    return s.isdigit() and len(s) >= 8


def apply_ref_to_designation(line, cfg):
    """Ajoute la reference a la designation si elle n'y figure pas deja ET que
    ce n'est pas un code-barres. Utile quand le fournisseur ne met qu'un libelle
    generique ('stylo') sans numero : la reference rejoint le nom pour identifier
    l'article et permettre le rapprochement par numero. Idempotent (ne double
    jamais la reference)."""
    if not cfg.get("ref_dans_designation", True):
        return
    ref = (line.get("ref_art") or "").strip()
    desig = (line.get("designation") or "").strip()
    if not ref or looks_like_barcode(ref):
        return
    nref = norm(ref)
    if nref and nref in norm(desig):
        return                      # reference deja presente dans le libelle
    line["designation"] = (desig + " " + ref).strip() if desig else ref


# Correspondance charset Firebird -> codec Python (pour assainir le texte).
_CHARSET_CODEC = {
    "WIN1252": "cp1252", "WIN1256": "cp1256", "WIN1250": "cp1250",
    "ISO8859_1": "latin-1", "ISO8859_15": "iso8859-15",
    "UTF8": "utf-8", "UNICODE_FSS": "utf-8", "ASCII": "ascii", "NONE": "cp1252",
}


def codec_for(charset):
    return _CHARSET_CODEC.get((charset or "WIN1252").upper(), "cp1252")


def safe_text(s, codec):
    """Rend une chaine ecrivable dans le charset de la base.
    Conserve les accents (presents dans le charset) et ne remplace que les
    caracteres impossibles a encoder (translitteration ASCII, sinon '?'),
    afin d'eviter l'echec total de l'ecriture (probleme d'encodage)."""
    if s is None:
        return None
    s = str(s)
    try:
        s.encode(codec)
        return s
    except (UnicodeEncodeError, LookupError):
        out = []
        for ch in s:
            try:
                ch.encode(codec)
                out.append(ch)
            except UnicodeEncodeError:
                repl = unicodedata.normalize("NFKD", ch).encode("ascii", "ignore").decode("ascii")
                out.append(repl if repl else "?")
        return "".join(out)


def round_price_up(value, tiers):
    """Arrondit un prix VERS LE HAUT par paliers selon sa grandeur.
    'tiers' = liste de [seuil_max, pas] ; un seuil_max null/None = au-dela.
    Ex. [[200, 5], [None, 10]] : < 200 -> au 5 superieur ; >= 200 -> au 10 superieur."""
    import math
    if value is None or value <= 0:
        return value
    for seuil, pas in tiers:
        if seuil is None or value < seuil:
            return int(math.ceil(value / pas) * pas)
    return value


def load_config(path):
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))  # copie profonde
    if path:
        with open(path, "r", encoding="utf-8") as fh:
            user_cfg = json.load(fh)
        for k, v in user_cfg.items():
            if isinstance(v, dict) and isinstance(cfg.get(k), dict):
                cfg[k].update(v)
            else:
                cfg[k] = v
    return cfg


# --------------------------------------------------------------------------- #
#  Lecture de l'Excel fournisseur
# --------------------------------------------------------------------------- #
def find_header_row(ws, aliases):
    """Trouve la ligne d'en-tete : celle qui contient la colonne 'ref_art'."""
    wanted = set(aliases["ref_art"])
    for r, row in enumerate(ws.iter_rows(values_only=True), start=1):
        cells = {norm(c) for c in row if c is not None}
        if cells & wanted:
            return r, list(row)
    return None, None


def map_columns(header, aliases):
    """Associe chaque champ logique a son index de colonne dans l'Excel."""
    index = {}
    normalized = [norm(h) for h in header]
    for field, names in aliases.items():
        for i, h in enumerate(normalized):
            if h in names:
                index[field] = i
                break
    return index


def read_excel(path, cfg):
    aliases = cfg["colonnes"]
    wb = openpyxl.load_workbook(path, data_only=True)
    ws = wb.active
    hrow, header = find_header_row(ws, aliases)
    if hrow is None:
        raise ValueError(
            "Impossible de trouver la ligne d'en-tete (colonne 'Ref. Art.' "
            "introuvable). Verifiez le fichier Excel.")
    colmap = map_columns(header, aliases)
    for req in ("ref_art", "qte"):
        if req not in colmap:
            raise ValueError("Colonne obligatoire manquante dans l'Excel : %s" % req)

    price_field = cfg.get("colonne_prix", "prix")
    if price_field not in colmap:
        raise ValueError(
            "Colonne de prix introuvable dans l'Excel (cherche : %s)."
            % ", ".join(aliases.get(price_field, [price_field])))

    codec = codec_for(cfg.get("charset"))
    txt = lambda v: safe_text(v, codec)
    lines = []
    for row in ws.iter_rows(min_row=hrow + 1, values_only=True):
        ref = row[colmap["ref_art"]] if colmap.get("ref_art") is not None else None
        if ref is None or str(ref).strip() == "":
            continue
        line = {
            "ref_art":     txt(str(ref).strip()),
            "designation": txt(str(row[colmap["designation"]]).strip()
                               if "designation" in colmap and row[colmap["designation"]] is not None
                               else str(ref).strip()),
            "qte":   to_float(row[colmap["qte"]], 0.0),
            "prix":  to_float(row[colmap[price_field]], 0.0),
            "tva":   (to_float(row[colmap["tva"]], cfg["default_tva"])
                      if "tva" in colmap else cfg["default_tva"]),
            "famille": txt(str(row[colmap["famille"]]).strip()
                           if "famille" in colmap and row[colmap["famille"]] is not None
                           else ""),
            "code_barres": txt(str(row[colmap["code_barres"]]).strip()
                               if "code_barres" in colmap and row[colmap["code_barres"]] is not None
                               else ""),
        }
        apply_ref_to_designation(line, cfg)
        lines.append(line)
    return lines


def load_lines_json(path, cfg):
    """Charge des lignes pre-analysees (memes cles que read_excel, depuis la
    table editee de la GUI) et les assainit EXACTEMENT comme read_excel
    (safe_text + to_float), pour que --lines et --excel produisent un import
    identique. Cles supplementaires optionnelles : prix_vente, maj_prix_achat."""
    with open(path, "r", encoding="utf-8") as fh:
        raw = json.load(fh)
    codec = codec_for(cfg.get("charset"))
    txt = lambda v: safe_text(v, codec)
    lines = []
    for d in raw:
        ref = d.get("ref_art")
        if ref is None or str(ref).strip() == "":
            continue
        line = {
            "ref_art":     txt(str(ref).strip()),
            "designation": txt(str(d.get("designation")).strip()
                               if d.get("designation") else str(ref).strip()),
            "qte":         to_float(d.get("qte"), 0.0),
            "prix":        to_float(d.get("prix"), 0.0),
            "tva":         to_float(d.get("tva"), cfg["default_tva"]),
            "famille":     txt(str(d.get("famille") or "").strip()),
            "code_barres": txt(str(d.get("code_barres") or "").strip()),
        }
        apply_ref_to_designation(line, cfg)
        pv = d.get("prix_vente")
        if pv is not None and str(pv).strip() != "":
            line["prix_vente"] = to_float(pv, None)
        if d.get("maj_prix_achat"):
            line["maj_prix_achat"] = True
        lines.append(line)
    return lines


def load_lines(args, cfg):
    """Charge les lignes depuis --excel OU --lines (une seule des deux)."""
    if getattr(args, "lines", None):
        if not os.path.isfile(args.lines):
            sys.exit("Fichier de lignes introuvable : %s" % args.lines)
        return load_lines_json(args.lines, cfg)
    if not os.path.isfile(args.excel):
        sys.exit("Fichier Excel introuvable : %s" % args.excel)
    return read_excel(args.excel, cfg)


# --------------------------------------------------------------------------- #
#  Rapprochement par designation (lignes sans code-barres)
# --------------------------------------------------------------------------- #
_NUM_RE = re.compile(r"\d+")
_SIZE_TOKEN_RE = re.compile(r"""^(
        \d{1,3}\s?[x*]\s?\d{1,3}(\s?[x*]\s?\d{1,3})?  |   # 24x32, 10x15x5
        a\d                                           |   # a4, a3, a5
        \d{1,3}(f|gr?|g|ml|cl|l|cm|mm|m|p|w|v)         |   # 10f, 9gr, 500ml, 80g
        \d{1,2}(eme|er|e)                                 # 1er, 2eme
    )$""", re.VERBOSE)


def number_tokens(text_norm):
    """Numeros 'code' candidats : suites de >= 3 chiffres (70010, 3578).
    Les tailles courtes (24, 10, 9) sont exclues -> evite les faux positifs."""
    return {m.group(0) for m in _NUM_RE.finditer(text_norm) if len(m.group(0)) >= 3}


def is_size_token(w):
    return bool(_SIZE_TOKEN_RE.match(w))


def word_tokens(text_norm):
    """Mots significatifs (>= 3 lettres), hors tokens taille/format."""
    out = []
    for w in text_norm.split():
        if is_size_token(w):
            continue
        ww = "".join(c for c in w if c.isalpha())
        if len(ww) >= 3:
            out.append(ww)
    return out


def build_article_index(cur):
    """Index memoire des articles existants par numero-token (une requete).
    Retourne (by_number, exact_refs, prix_achat_by_ref)."""
    cur.execute("SELECT REF_ART, DESIGNATION, PRIXACHATHT FROM ARTICLE")
    by_number, exact_refs, prix = {}, set(), {}
    for ref, desig, pa in cur.fetchall():
        exact_refs.add(ref)
        prix[ref] = pa
        dnorm = norm(desig)
        nums = number_tokens(dnorm)
        if not nums:
            continue
        lead = set(word_tokens(dnorm)[:3])
        entry = (ref, desig, lead)
        for n in nums:
            by_number.setdefault(n, []).append(entry)
    return by_number, exact_refs, prix


def best_match_for_line(line, by_number, exact_refs, prix, min_score=0.60):
    """Pour une ligne fournisseur, retourne le rapprochement {ref_exists,
    match_ref, match_designation, match_score, match_prix_achat, status}."""
    ref = line["ref_art"]
    if ref in exact_refs:
        return {"ref_exists": True, "match_ref": ref, "match_designation": None,
                "match_score": 1.0, "match_prix_achat": prix.get(ref),
                "status": "exact"}

    src = norm(line.get("designation") or line["ref_art"])
    src_nums = number_tokens(src)
    src_lead = set(word_tokens(src)[:3])
    miss = {"ref_exists": False, "match_ref": None, "match_designation": None,
            "match_score": 0.0, "match_prix_achat": None, "status": "new"}
    if not src_nums:
        return miss
    best = None
    for n in src_nums:
        for cand_ref, cand_desig, cand_lead in by_number.get(n, ()):
            overlap = (len(src_lead & cand_lead) / len(src_lead)) if src_lead else 0.0
            score = 0.55 + 0.45 * overlap
            if best is None or score > best[0]:
                best = (score, cand_ref, cand_desig)
    if best and best[0] >= min_score:
        return {"ref_exists": False, "match_ref": best[1],
                "match_designation": best[2], "match_score": round(best[0], 3),
                "match_prix_achat": prix.get(best[1]), "status": "matched"}
    return miss


# --------------------------------------------------------------------------- #
#  Operations base de donnees
# --------------------------------------------------------------------------- #
class Importer:
    def __init__(self, con, cfg):
        self.con = con
        self.cfg = cfg
        self.cur = con.cursor()
        self._famille_cache = None
        self._fam_code_next = None
        self.codec = codec_for(cfg.get("charset"))
        # Suivi des codes-barres pour respecter l'index UNIQUE CODE_BARRES :
        # un barcode deja attribue dans cet import ou porte par un autre article
        # est ignore (au lieu de faire echouer toute la transaction).
        self._barcodes_used = set()
        self.barcode_added = set()  # refs ayant recu un code-barres (EQUIV / ARTICLE)
        self.barcode_skipped = []   # (ref, barcode) ignores (doublon / conflit UNIQUE)
        # coefficients reels du type de piece (PIECE et ITEM)
        self.coeff_piece, self.coeff_piece_tr, \
            self.coeff_item, self.coeff_item_tr = self._load_type_coeffs()

    # -- helpers ----------------------------------------------------------
    def _load_type_coeffs(self):
        """Lit les coefficients du type de piece (defaut : reception +1)."""
        self.cur.execute(
            "SELECT COEFF_PIECE, COEFF_PIECE_TR, COEFF_ITEM, COEFF_ITEM_TR "
            "FROM LOCAL_TYPE_PIECE WHERE CODE_TYPE_PIECE = ?",
            (self.cfg["code_type_piece"],))
        row = self.cur.fetchone()
        if not row:
            return 1, 0, 1, 0
        return (row[0] if row[0] is not None else 1,
                row[1] if row[1] is not None else 0,
                row[2] if row[2] is not None else 1,
                row[3] if row[3] is not None else 0)

    def gen_value(self, generator):
        self.cur.execute("SELECT GEN_ID(%s, 0) FROM RDB$DATABASE" % generator)
        return self.cur.fetchone()[0] or 0

    def advance_generator(self, generator, target):
        """Avance le generateur jusqu'a 'target' (jamais en arriere)."""
        cur = self.gen_value(generator)
        if target > cur:
            self.cur.execute("SELECT GEN_ID(%s, %d) FROM RDB$DATABASE"
                             % (generator, target - cur))
            self.cur.fetchone()

    def next_base(self, generator, table, col):
        """Base de numerotation = max(MAX numerique existant, generateur).
        Reproduit le 'MAX+1' du logiciel tout en restant au-dessus du
        generateur, en ignorant les plages reservees (> seuil)."""
        threshold = int(self.cfg.get("reserved_id_threshold", 1000000))
        self.cur.execute(
            "SELECT MAX(CAST(%s AS BIGINT)) FROM %s "
            "WHERE %s SIMILAR TO '[0-9]+' AND CHAR_LENGTH(%s) <= 15 "
            "  AND CAST(%s AS BIGINT) < ?" % (col, table, col, col, col),
            (threshold,))
        mx = self.cur.fetchone()[0] or 0
        return max(int(mx), int(self.gen_value(generator)))

    def exists(self, sql, params):
        self.cur.execute(sql, params)
        return self.cur.fetchone() is not None

    # -- referentiels (familles / unites / tiers) -------------------------
    def ensure_famille(self, code, intitule):
        if not self.exists("SELECT 1 FROM FAMILLE WHERE CODEFAMILLE = ?", (code,)):
            self.cur.execute(
                "INSERT INTO FAMILLE (CODEFAMILLE, INTITULE, TAUX_TVA) VALUES (?, ?, ?)",
                (code, safe_text(intitule, self.codec)[:50], self.cfg["default_tva"]))

    def ensure_unite(self, code, intitule):
        if code and not self.exists("SELECT 1 FROM UNITE WHERE CODE_UNITE = ?", (code,)):
            self.cur.execute(
                "INSERT INTO UNITE (CODE_UNITE, INTITULE, FACTEUR) VALUES (?, ?, 1)",
                (code, safe_text(intitule, self.codec)[:30]))

    def fam_tiers_code(self, intitule_contains, default):
        """Code de la famille-tiers dont l'intitule contient 'intitule_contains'
        (ex: 'Fournisseur' -> 'FO', 'Dep' -> 'DP'). Repli sur 'default'. Cache.
        Prefere le noeud dont l'intitule COMMENCE par le terme (plus specifique),
        avant le premier noeud qui le CONTIENT (evite CF vs FO)."""
        if not hasattr(self, "_famtiers_cache"):
            self._famtiers_cache = {}
        key = intitule_contains.lower()
        if key not in self._famtiers_cache:
            needle = intitule_contains.upper()
            # D'abord : intitule qui commence par le terme (noeud feuille/specifique)
            self.cur.execute(
                "SELECT CODE_FAM_TIERS FROM FAM_TIERS "
                "WHERE UPPER(INTITULE) STARTING WITH ? ROWS 1",
                (needle,))
            row = self.cur.fetchone()
            if not row:
                # Repli : intitule qui contient le terme
                self.cur.execute(
                    "SELECT CODE_FAM_TIERS FROM FAM_TIERS "
                    "WHERE UPPER(INTITULE) CONTAINING ? ROWS 1",
                    (needle,))
                row = self.cur.fetchone()
            self._famtiers_cache[key] = row[0] if row else default
        return self._famtiers_cache[key]

    def ensure_tiers(self, code, raison, role=None):
        """Cree le tiers s'il manque. 'role' = 'fournisseur' ou 'depot' : fixe
        CODE_FAM_TIERS sur la bonne famille-tiers (FO / DP) pour que le tiers
        apparaisse dans les listes du logiciel."""
        if not code or self.exists("SELECT 1 FROM TIERS WHERE CODE_TIERS = ?", (code,)):
            return
        if role == "depot":
            fam = self.fam_tiers_code("dep", "DP")
        elif role == "fournisseur":
            fam = self.fam_tiers_code("fournisseur", "FO")
        else:
            fam = None
        self.cur.execute(
            "INSERT INTO TIERS (CODE_TIERS, CODE_FAM_TIERS, RAISON_SOCIALE, DATE_CREATION) "
            "VALUES (?, ?, ?, ?)",
            (code, fam, safe_text(raison or code, self.codec)[:200],
             datetime.datetime.now()))

    def _load_famille_cache(self):
        if self._famille_cache is None:
            self.cur.execute("SELECT CODEFAMILLE, INTITULE FROM FAMILLE")
            self._famille_cache = {norm(i): c for c, i in self.cur.fetchall()}

    def _next_famille_code(self):
        """Genere un code famille numerique unique (max numerique + 1)."""
        if self._fam_code_next is None:
            self.cur.execute(
                "SELECT MAX(CAST(CODEFAMILLE AS INTEGER)) FROM FAMILLE "
                "WHERE CODEFAMILLE SIMILAR TO '[0-9]+' AND CHAR_LENGTH(CODEFAMILLE) <= 9")
            self._fam_code_next = int(self.cur.fetchone()[0] or 0)
        while True:
            self._fam_code_next += 1
            code = str(self._fam_code_next)
            if not self.exists("SELECT 1 FROM FAMILLE WHERE CODEFAMILLE = ?", (code,)):
                return code

    def _root_famille(self):
        """Code d'une famille racine existante (CODEFAMILLE_M NULL), pour y
        rattacher les nouvelles familles afin qu'elles soient visibles dans
        l'arbre du logiciel. Repli sur default_famille."""
        default = self.cfg["default_famille"]
        if self.exists("SELECT 1 FROM FAMILLE WHERE CODEFAMILLE = ?", (default,)):
            return default
        self.cur.execute(
            "SELECT CODEFAMILLE FROM FAMILLE WHERE CODEFAMILLE_M IS NULL "
            "ORDER BY CODEFAMILLE ROWS 1")
        row = self.cur.fetchone()
        return row[0] if row else None

    def create_famille(self, label):
        """Cree une famille portant le NOM (intitule) du libelle Excel, avec un
        code genere automatiquement, rattachee a la racine et VISIBLE."""
        cfg = self.cfg
        code = self._next_famille_code()
        parent = self._root_famille()
        self.cur.execute(
            "INSERT INTO FAMILLE (CODEFAMILLE, CODEFAMILLE_M, INTITULE, TAUX_TVA, "
            " BOUTIQ_VISIBLE) VALUES (?, ?, ?, ?, 1)",
            (code, parent, safe_text(label, self.codec)[:50], cfg["default_tva"]))
        self._famille_cache[norm(label)] = code     # eviter les doublons
        return code

    def resolve_famille(self, label):
        """Associe le libelle 'Famille' de l'Excel a une famille EXISTANTE par
        son NOM (intitule). Si aucune ne correspond : cree la famille par son
        nom (si create_missing_familles), sinon retourne la famille par defaut."""
        cfg = self.cfg
        if not label:
            return cfg["default_famille"]
        if cfg.get("match_famille_par_intitule"):
            self._load_famille_cache()
            code = self._famille_cache.get(norm(label))
            if code:
                return code
        if cfg.get("create_missing_familles"):
            self._load_famille_cache()
            code = self._famille_cache.get(norm(label))
            return code if code else self.create_famille(label)
        return cfg["default_famille"]

    # -- articles ---------------------------------------------------------
    def _barcode_for_line(self, line, ref):
        """Code-barres souhaite pour la ligne (colonne Excel, ou la reference si
        barcode_depuis_ref). Vide -> None."""
        barcode = (line.get("code_barres") or "").strip()
        if not barcode and self.cfg.get("barcode_depuis_ref"):
            barcode = ref
        return barcode[:60] or None

    def _barcode_is_free(self, barcode, ref):
        """Vrai si 'barcode' peut etre attribue a 'ref' sans violer l'index
        UNIQUE CODE_BARRES de ARTICLE : pas deja utilise dans cet import, et pas
        deja porte par un AUTRE article."""
        if not barcode or barcode in self._barcodes_used:
            return False
        self.cur.execute("SELECT REF_ART FROM ARTICLE WHERE CODE_BARRES = ?", (barcode,))
        row = self.cur.fetchone()
        return not (row and row[0] != ref)

    def add_barcode_equiv(self, ref, barcode):
        """Ajoute le code-barres dans EQUIV_CBARRES — la table que le logiciel
        AFFICHE dans la fiche article. Idempotent ; respecte la FK (l'article
        doit exister) et evite d'attribuer un meme code a deux articles.
        Retourne True si une ligne a ete ajoutee."""
        if not barcode:
            return False
        if self.exists("SELECT 1 FROM EQUIV_CBARRES WHERE REF_ART = ? AND CODE_BARRES = ?",
                       (ref, barcode)):
            return False
        self.cur.execute("SELECT REF_ART FROM EQUIV_CBARRES WHERE CODE_BARRES = ?", (barcode,))
        row = self.cur.fetchone()
        if row and row[0] != ref:
            self.barcode_skipped.append((ref, barcode))
            return False
        noequiv = str(self.next_base("NEXTEQUIV_CBARRES", "EQUIV_CBARRES",
                                     "NOEQUIV_CBARRES") + 1)
        self.cur.execute(
            "INSERT INTO EQUIV_CBARRES (NOEQUIV_CBARRES, REF_ART, CODE_BARRES) "
            "VALUES (?, ?, ?)", (noequiv, ref, barcode))
        self.advance_generator("NEXTEQUIV_CBARRES", int(noequiv))
        return True

    def sync_barcode(self, ref, barcode):
        """Renseigne le code-barres d'un article EXISTANT : EQUIV_CBARRES (table
        affichee) + ARTICLE.CODE_BARRES si vide (cle de scan, index UNIQUE)."""
        if not barcode:
            return
        added = False
        self.cur.execute("SELECT CODE_BARRES FROM ARTICLE WHERE REF_ART = ?", (ref,))
        r = self.cur.fetchone()
        cur_bc = r[0] if r else None
        if not (cur_bc and str(cur_bc).strip()) and self._barcode_is_free(barcode, ref):
            self.cur.execute(
                "UPDATE ARTICLE SET CODE_BARRES = ?, CODE_BARRE = ? WHERE REF_ART = ?",
                (barcode, barcode[:35], ref))
            self._barcodes_used.add(barcode)
            added = True
        if self.add_barcode_equiv(ref, barcode):
            added = True
        if added:
            self.barcode_added.add(ref)

    def upsert_article(self, line):
        """Cree l'article s'il n'existe pas. Retourne 'created', 'exists' ou
        'updated' (article existant dont le prix d'achat a ete mis a jour).
        Renseigne aussi le code-barres (EQUIV_CBARRES + ARTICLE) a la creation
        comme en backfill sur un article existant (maj_code_barres)."""
        ref = line["ref_art"]
        cfg = self.cfg
        tva = line["tva"]
        prix_achat_ht = line["prix"]              # prix de vente fournisseur = notre prix d'achat
        prix_achat_ttc = prix_achat_ht if cfg.get("calc_prix_achat_ttc", True) else None
        barcode = self._barcode_for_line(line, ref)

        if self.exists("SELECT 1 FROM ARTICLE WHERE REF_ART = ?", (ref,)):
            # Article existant : on n'y touche pas, SAUF demande explicite de mise
            # a jour du prix d'achat, et/ou ajout d'un code-barres absent.
            state = "exists"
            if line.get("maj_prix_achat") and prix_achat_ht > 0:
                self.cur.execute(
                    "UPDATE ARTICLE SET PRIXACHATHT = ?, PRIXACHATTTC = ? WHERE REF_ART = ?",
                    (prix_achat_ht, prix_achat_ttc, ref))
                state = "updated"
            if barcode and cfg.get("maj_code_barres", True):
                self.sync_barcode(ref, barcode)
            return state

        codefamille = self.resolve_famille(line["famille"])

        # Prix de vente : valeur saisie a la main (override) sinon calcul auto.
        prix_vente_ht = prix_vente_ttc = None
        override = line.get("prix_vente")
        if override is not None and float(override) > 0:
            prix_vente_ht = round(float(override), 4)
        elif cfg.get("prix_vente_auto"):
            brut = prix_achat_ht * (1 + cfg.get("marge_pct", 50) / 100.0)
            prix_vente_ht = round_price_up(brut, cfg.get("arrondi_paliers",
                                                         [[200, 5], [1000, 10], [None, 50]]))
        if prix_vente_ht is not None:
            prix_vente_ttc = round(prix_vente_ht * (1 + tva / 100.0), 4)

        # Code-barres : ARTICLE.CODE_BARRES (cle de scan, index UNIQUE -> on ne
        # l'ecrit que s'il est libre) ET EQUIV_CBARRES (la table que le logiciel
        # AFFICHE dans la fiche article, ajoutee juste apres la creation).
        if barcode and self._barcode_is_free(barcode, ref):
            code_barres = barcode[:60]
            code_barre = barcode[:35]
            self._barcodes_used.add(barcode)
        else:
            if barcode:
                self.barcode_skipped.append((ref, barcode))
            code_barres = code_barre = None

        # Unite de base : vide par defaut (comme le logiciel)
        unite = cfg.get("default_unite") or None
        # Fournisseur de l'article = fournisseur du bon (chaque article retient
        # d'ou il vient).
        code_fourn = cfg.get("code_tiers") or None

        self.cur.execute(
            "INSERT INTO ARTICLE "
            "(REF_ART, CODEFAMILLE, DESIGNATION, CODE_BARRES, CODE_BARRE, CODE_FOURN, "
            " PRIXACHATHT, PRIXACHATTTC, PRIXVENTEHT, PRIXVENTETTC, TAUX_TVA, "
            " CODE_UNITE_BASE, CODE_UNITE_AC, CODE_UNITE_VE, DATE_CREATION) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (ref, codefamille, line["designation"][:100], code_barres, code_barre, code_fourn,
             prix_achat_ht, prix_achat_ttc, prix_vente_ht, prix_vente_ttc, tva,
             unite, unite, unite, datetime.datetime.now()))
        # Code-barres visible dans la fiche article (EQUIV_CBARRES).
        if barcode and self.add_barcode_equiv(ref, barcode):
            self.barcode_added.add(ref)
        return "created"

    # -- piece + lignes ---------------------------------------------------
    def create_piece(self, nopiece, date_piece):
        cfg = self.cfg
        refdoc = safe_text(cfg.get("refdoc") or "", self.codec)[:255] or None
        self.cur.execute(
            "INSERT INTO PIECE "
            "(NOPIECE, CODE_TYPE_PIECE, CODE_TIERS, CODE_DEPOT, DATEPIECE, REFDOC, "
            " ETAT, ANNULEE, COEFF, COEFF_TR, MONTANT, MONTANTHT, MONTANTTTC, TVA, "
            " USERNAME) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, ?, 0, 0, 0, 0, ?)",
            (nopiece, cfg["code_type_piece"],
             cfg["code_tiers"] or None, cfg["code_depot"] or None,
             date_piece, refdoc, cfg.get("etat") or None,
             self.coeff_piece, self.coeff_piece_tr, cfg.get("user")))
        return nopiece

    def add_item(self, noitem, nopiece, line, date_piece):
        cfg = self.cfg
        unite = cfg.get("default_unite") or None
        self.cur.execute(
            "INSERT INTO ITEM "
            "(NOITEM, NOPIECE, REF_ART, QTE, PRIXHT, TVA, COEFF, COEFF_TR, "
            " CODE_UNITE, CODE_TIERS, CODE_DEPOT, DATEPIECE) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (noitem, nopiece, line["ref_art"], line["qte"], line["prix"],
             line["tva"], self.coeff_item, self.coeff_item_tr, unite,
             cfg["code_tiers"] or None, cfg["code_depot"] or None, date_piece))
        return noitem

    def update_totaux(self, nopiece, montant_ht, tva, montant_ttc):
        self.cur.execute(
            "UPDATE PIECE SET MONTANTHT = ?, TVA = ?, MONTANTTTC = ?, MONTANT = ? "
            "WHERE NOPIECE = ?",
            (montant_ht, tva, montant_ttc, montant_ttc, nopiece))

    def get_ref_piece(self, nopiece):
        self.cur.execute("SELECT REF_PIECE FROM PIECE WHERE NOPIECE = ?", (nopiece,))
        row = self.cur.fetchone()
        return row[0] if row else None


# --------------------------------------------------------------------------- #
#  Programme principal
# --------------------------------------------------------------------------- #
def connect(cfg):
    # Permet de pointer explicitement vers la librairie cliente Firebird
    # (ex: fbclient.dll sur Windows, ou libfbembed pour un acces embedded).
    lib = cfg.get("fb_client_library")
    if lib:
        fdb.load_api(lib)
    kwargs = dict(database=cfg["database"], user=cfg["user"],
                  password=cfg["password"], charset=cfg.get("charset", "WIN1256"))
    host = cfg.get("host")
    if host:
        kwargs["host"] = host
        if cfg.get("port"):
            kwargs["port"] = int(cfg["port"])
    return fdb.connect(**kwargs)


def _write_json(out_path, data):
    with open(out_path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False)


def mode_match(cfg, lines, out_path):
    """Calcule les correspondances par designation et ecrit le JSON augmente."""
    auto = cfg.get("match_par_designation", True)
    con = connect(cfg)
    try:
        cur = con.cursor()
        by_number, exact_refs, prix = build_article_index(cur)
        min_score = float(cfg.get("match_min_score", 0.60))
        result = []
        for ln in lines:
            if auto:
                m = best_match_for_line(ln, by_number, exact_refs, prix, min_score)
            elif ln["ref_art"] in exact_refs:
                m = {"ref_exists": True, "match_ref": ln["ref_art"],
                     "match_designation": None, "match_score": 1.0,
                     "match_prix_achat": prix.get(ln["ref_art"]), "status": "exact"}
            else:
                m = {"ref_exists": False, "match_ref": None, "match_designation": None,
                     "match_score": 0.0, "match_prix_achat": None, "status": "new"}
            row = dict(ln); row.update(m); result.append(row)
    finally:
        con.close()
    _write_json(out_path, result)
    ne = sum(r["status"] == "exact" for r in result)
    nm = sum(r["status"] == "matched" for r in result)
    nn = sum(r["status"] == "new" for r in result)
    print("Rapprochement : exact=%d matched=%d new=%d -> %s" % (ne, nm, nn, out_path))


def mode_check_dup(cfg, lines, out_path):
    """Cherche des bons de reception ACTIFS deja presents (meme N° de bon, ou
    meme fournisseur + meme total), pour eviter un double import."""
    total_ht = round(sum(ln["qte"] * ln["prix"] for ln in lines), 2)
    refdoc = (cfg.get("refdoc") or "").strip()
    tiers = (cfg.get("code_tiers") or "").strip()
    con = connect(cfg)
    try:
        cur = con.cursor()
        dups = []
        seen = set()
        if refdoc:
            cur.execute(
                "SELECT NOPIECE, REF_PIECE, DATEPIECE, MONTANTHT, REFDOC, CODE_TIERS "
                "FROM PIECE WHERE CODE_TYPE_PIECE = ? AND ANNULEE = 1 AND REFDOC = ?",
                (cfg["code_type_piece"], refdoc))
            for row in cur.fetchall():
                seen.add(row[0]); dups.append(_dup_row(row, "meme N° de bon"))
        if tiers:
            cur.execute(
                "SELECT NOPIECE, REF_PIECE, DATEPIECE, MONTANTHT, REFDOC, CODE_TIERS "
                "FROM PIECE WHERE CODE_TYPE_PIECE = ? AND ANNULEE = 1 AND CODE_TIERS = ? "
                "AND ABS(COALESCE(MONTANTHT,0) - ?) <= 1",
                (cfg["code_type_piece"], tiers, total_ht))
            for row in cur.fetchall():
                if row[0] not in seen:
                    seen.add(row[0]); dups.append(_dup_row(row, "meme fournisseur + meme total"))
    finally:
        con.close()
    _write_json(out_path, {"total_ht": total_ht, "duplicates": dups})
    print("Doublons potentiels : %d -> %s" % (len(dups), out_path))


def _dup_row(row, motif):
    return {"nopiece": row[0], "ref_piece": row[1],
            "date": str(row[2]) if row[2] else None,
            "montant_ht": row[3], "refdoc": row[4], "code_tiers": row[5], "motif": motif}


def mode_list_familles(cfg, out_path):
    """Liste les familles existantes, ordonnees en arbre (parent avant enfants)."""
    con = connect(cfg)
    try:
        cur = con.cursor()
        cur.execute("SELECT CODEFAMILLE, CODEFAMILLE_M, INTITULE FROM FAMILLE")
        rows = cur.fetchall()
    finally:
        con.close()
    children = {}
    intitule = {}
    for code, parent, lib in rows:
        intitule[code] = lib or code
        children.setdefault(parent, []).append(code)
    out = []

    def walk(parent, depth):
        for code in sorted(children.get(parent, []), key=lambda c: norm(intitule.get(c, c))):
            out.append({"code": code, "intitule": intitule.get(code, code), "depth": depth})
            walk(code, depth + 1)
    walk(None, 0)
    # familles dont le parent n'existe pas (orphelines) : ajoutees a plat
    placed = {o["code"] for o in out}
    for code in intitule:
        if code not in placed:
            out.append({"code": code, "intitule": intitule[code], "depth": 0})
    _write_json(out_path, out)
    print("Familles : %d -> %s" % (len(out), out_path))


def mode_list_tiers(cfg, out_path):
    """Liste fournisseurs (sous-arbre FO) et depots (sous-arbre DP)."""
    con = connect(cfg)
    try:
        cur = con.cursor()
        cur.execute("SELECT CODE_FAM_TIERS, CODE_FAM_TIERS_M, INTITULE FROM FAM_TIERS")
        fam = cur.fetchall()
        kids = {}
        root_by_intit = {}
        for code, parent, lib in fam:
            kids.setdefault(parent, []).append(code)
            if lib:
                root_by_intit[code] = lib

        def descendants(root):
            seen, stack = set(), [root]
            while stack:
                c = stack.pop()
                if c in seen:
                    continue
                seen.add(c)
                stack.extend(kids.get(c, []))
            return seen

        def find_root(contains, default):
            # Prefere le noeud dont l'intitule COMMENCE par le mot-cle (plus specifique)
            # plutot qu'un noeud parent dont l'intitule le CONTIENT (ex: CF vs FO).
            starts = [(code, lib) for code, _p, lib in fam
                      if lib and lib.upper().startswith(contains)]
            if starts:
                return starts[0][0]
            for code, _p, lib in fam:
                if lib and contains in lib.upper():
                    return code
            return default
        fo = find_root("FOURNISSEUR", "FO")
        dp = find_root("DEP", "DP")
        fo_set, dp_set = descendants(fo), descendants(dp)

        def fetch(fam_codes):
            if not fam_codes:
                return []
            qs = ",".join("?" * len(fam_codes))
            cur.execute("SELECT CODE_TIERS, RAISON_SOCIALE FROM TIERS "
                        "WHERE CODE_FAM_TIERS IN (%s) ORDER BY RAISON_SOCIALE" % qs,
                        tuple(fam_codes))
            return [{"code": c, "raison": r or c} for c, r in cur.fetchall()]
        data = {"fournisseurs": fetch(fo_set), "depots": fetch(dp_set),
                "code_fam_fournisseur": fo, "code_fam_depot": dp}
    finally:
        con.close()
    _write_json(out_path, data)
    print("Fournisseurs : %d, Depots : %d -> %s"
          % (len(data["fournisseurs"]), len(data["depots"]), out_path))


def mode_sync_barcodes(cfg, out_path=None):
    """Repare les articles DEJA importes : pour chaque article, lit les DEUX
    champs ARTICLE.CODE_BARRES (60) ET ARTICLE.CODE_BARRE (35), et alimente
    EQUIV_CBARRES (la table que le logiciel affiche dans la fiche article) avec
    chaque code-barres trouve. Complete aussi le champ ARTICLE vide a partir de
    l'autre. Idempotent : relancer n'ajoute pas de doublon."""
    con = connect(cfg)
    imp = Importer(con, cfg)
    try:
        cur = con.cursor()
        cur.execute(
            "SELECT REF_ART, CODE_BARRES, CODE_BARRE FROM ARTICLE "
            "WHERE (CODE_BARRES IS NOT NULL AND CHAR_LENGTH(TRIM(CODE_BARRES)) > 0) "
            "   OR (CODE_BARRE  IS NOT NULL AND CHAR_LENGTH(TRIM(CODE_BARRE))  > 0)")
        rows = cur.fetchall()
        for ref, bc1, bc2 in rows:
            seen = set()
            for bc in (bc1, bc2):                 # les DEUX champs
                bc = (bc or "").strip()
                if bc and bc not in seen:
                    seen.add(bc)
                    # sync_barcode : ajoute dans EQUIV_CBARRES + complete le champ
                    # ARTICLE vide (CODE_BARRES/CODE_BARRE) si libre.
                    imp.sync_barcode(ref, bc)
        con.commit()
        added = len(imp.barcode_added)
        msg = ("Synchronisation codes-barres : %d article(s) avec code-barres ; "
               "%d ajoute(s) dans EQUIV_CBARRES ; %d ignore(s)."
               % (len(rows), added, len(imp.barcode_skipped)))
        print(msg)
        if out_path:
            _write_json(out_path, {"sources": len(rows), "added": added,
                                   "skipped": len(imp.barcode_skipped)})
    except Exception as exc:
        con.rollback()
        print("ECHEC synchronisation : %s" % exc, file=sys.stderr); sys.exit(1)
    finally:
        con.close()


def mode_apply_barcodes(cfg, lines, out_path=None):
    """Applique les codes-barres de 'lines' (un Excel fournisseur, ou la table de
    la GUI) aux articles DEJA EXISTANTS — EQUIV_CBARRES (table affichee) +
    ARTICLE.CODE_BARRES si vide — SANS creer de bon de reception ni toucher au
    stock. Sert a reparer des articles importes avant la gestion des codes-barres :
    re-fournir le meme fichier applique uniquement les codes-barres.
    Rapproche par REFERENCE ; un article introuvable est signale, pas cree."""
    con = connect(cfg)
    imp = Importer(con, cfg)
    try:
        applied = notfound = nobc = 0
        notfound_refs = []
        for ln in lines:
            ref = ln.get("ref_art")
            barcode = imp._barcode_for_line(ln, ref)
            if not barcode:
                nobc += 1
                continue
            if not imp.exists("SELECT 1 FROM ARTICLE WHERE REF_ART = ?", (ref,)):
                notfound += 1
                if len(notfound_refs) < 20:
                    notfound_refs.append(ref)
                continue
            before = len(imp.barcode_added)
            imp.sync_barcode(ref, barcode)
            if len(imp.barcode_added) > before:
                applied += 1
        con.commit()
        print("Application codes-barres (sans bon, sans stock) :")
        print("  appliques        : %d" % applied)
        print("  sans code-barres : %d" % nobc)
        print("  articles introuvables : %d %s"
              % (notfound, notfound_refs if notfound_refs else ""))
        print("  ignores (doublon/conflit) : %d" % len(imp.barcode_skipped))
        if out_path:
            _write_json(out_path, {"applied": applied, "no_barcode": nobc,
                                   "not_found": notfound,
                                   "not_found_refs": notfound_refs,
                                   "skipped": len(imp.barcode_skipped)})
    except Exception as exc:
        con.rollback()
        print("ECHEC application codes-barres : %s" % exc, file=sys.stderr); sys.exit(1)
    finally:
        con.close()


def mode_cancel_piece(cfg, nopiece):
    """Annule un bon (ANNULEE=0) : le trigger UPDATE_PIECE repercute sur les
    items et le stock est repris (annulation native du logiciel)."""
    con = connect(cfg)
    try:
        cur = con.cursor()
        cur.execute("SELECT CODE_TYPE_PIECE, ANNULEE FROM PIECE WHERE NOPIECE = ?", (nopiece,))
        row = cur.fetchone()
        if not row:
            con.close(); sys.exit("Bon introuvable : NOPIECE=%s" % nopiece)
        cur.execute("UPDATE PIECE SET ANNULEE = 0 WHERE NOPIECE = ?", (nopiece,))
        con.commit()
        print("Bon NOPIECE=%s annule (stock repris)." % nopiece)
    except Exception as exc:
        con.rollback()
        print("ECHEC annulation : %s" % exc, file=sys.stderr); sys.exit(1)
    finally:
        con.close()


def make_template(path):
    """Ecrit un modele Excel avec les colonnes reconnues par l'outil."""
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Bon"
    ws.append(["Ref. Art.", "Désignation", "QTE", "Prix HT", "TVA", "Famille", "Code barres"])
    ws.append(["", "ensemble kit tracage 70010", 10, 120, 0, "SCOLAIRE", ""])
    wb.save(path)
    print("Modele Excel cree : %s" % path)


def main():
    ap = argparse.ArgumentParser(
        description="Importe un Excel fournisseur comme Bon de reception dans la base Firebird.")
    ap.add_argument("--config", help="Fichier de configuration JSON")
    src = ap.add_mutually_exclusive_group()
    src.add_argument("--excel", help="Fichier Excel du fournisseur (.xlsx)")
    src.add_argument("--lines", help="Fichier JSON de lignes pre-analysees (table GUI editee)")
    ap.add_argument("--db", help="Chemin du .FDB (surcharge la config)")
    ap.add_argument("--date", help="Date du bon (AAAA-MM-JJ). Defaut : aujourd'hui.")
    ap.add_argument("--code-tiers", help="Code fournisseur (surcharge la config)")
    ap.add_argument("--refdoc", help="N° du bon fournisseur (surcharge la config)")
    ap.add_argument("--dry-run", action="store_true",
                    help="Simulation : affiche le resultat sans rien ecrire en base.")
    # Modes auxiliaires (lecture seule sauf --cancel-piece)
    ap.add_argument("--match", action="store_true", help="Mode rapprochement -> --out")
    ap.add_argument("--check-dup", action="store_true", help="Verifie les doublons -> --out")
    ap.add_argument("--list-familles", action="store_true", help="Liste les familles -> --out")
    ap.add_argument("--list-tiers", action="store_true", help="Liste fournisseurs/depots -> --out")
    ap.add_argument("--cancel-piece", help="Annule le bon NOPIECE donne (ANNULEE=0)")
    ap.add_argument("--sync-barcodes", action="store_true",
                    help="Repare les articles deja importes : copie CODE_BARRES -> EQUIV_CBARRES")
    ap.add_argument("--apply-barcodes", action="store_true",
                    help="Applique les codes-barres d'un Excel/--lines aux articles existants "
                         "(sans bon, sans stock)")
    ap.add_argument("--make-template", help="Cree un modele Excel au chemin donne")
    ap.add_argument("--out", help="Fichier JSON de sortie (modes --match/--check-dup/--list-*)")
    args = ap.parse_args()

    # Modes ne necessitant pas de lignes
    if args.make_template:
        make_template(args.make_template); return

    cfg = load_config(args.config)
    if args.db:
        cfg["database"] = args.db
    if args.code_tiers is not None:
        cfg["code_tiers"] = args.code_tiers
    if args.refdoc is not None:
        cfg["refdoc"] = args.refdoc

    if args.cancel_piece:
        mode_cancel_piece(cfg, args.cancel_piece); return
    if args.sync_barcodes:
        mode_sync_barcodes(cfg, args.out); return
    if args.list_familles:
        if not args.out: sys.exit("--list-familles requiert --out.")
        mode_list_familles(cfg, args.out); return
    if args.list_tiers:
        if not args.out: sys.exit("--list-tiers requiert --out.")
        mode_list_tiers(cfg, args.out); return

    # Modes / import necessitant des lignes
    if not args.excel and not args.lines:
        sys.exit("Indiquez --excel ou --lines (ou un mode --list-*/--cancel-piece/--make-template).")
    lines = load_lines(args, cfg)
    if not lines:
        sys.exit("Aucune ligne d'article trouvee.")

    if args.match:
        if not args.out: sys.exit("--match requiert --out.")
        mode_match(cfg, lines, args.out); return
    if args.check_dup:
        if not args.out: sys.exit("--check-dup requiert --out.")
        mode_check_dup(cfg, lines, args.out); return
    if args.apply_barcodes:
        mode_apply_barcodes(cfg, lines, args.out); return

    print("Lignes lues dans l'Excel : %d" % len(lines))

    if args.date:
        date_piece = datetime.datetime.strptime(args.date, "%Y-%m-%d")
    else:
        date_piece = datetime.datetime.now()

    # 2) Connexion + import (transaction unique)
    con = connect(cfg)
    imp = Importer(con, cfg)
    try:
        # referentiels minimaux  (PIECE.USERNAME = utilisateur de connexion)
        imp.ensure_famille(cfg["default_famille"], cfg["default_famille_intitule"])
        imp.ensure_unite(cfg.get("default_unite"), cfg["default_unite_intitule"])
        if cfg.get("create_missing_tiers"):
            imp.ensure_tiers(cfg.get("code_tiers"), cfg.get("raison_sociale"), "fournisseur")
            imp.ensure_tiers(cfg.get("code_depot"), cfg.get("code_depot"), "depot")

        # numerotation collision-safe (MAX existant ou generateur)
        nopiece = str(imp.next_base("NEXTPIECE", "PIECE", "NOPIECE") + 1)
        item_no = imp.next_base("NEXTITEM", "ITEM", "NOITEM")

        imp.create_piece(nopiece, date_piece)

        created, existing, updated = [], [], []
        montant_ht = tva_tot = 0.0
        for line in lines:
            state = imp.upsert_article(line)
            if state == "created":
                created.append(line["ref_art"])
            elif state == "updated":
                updated.append(line["ref_art"])
            else:
                existing.append(line["ref_art"])
            item_no += 1
            imp.add_item(str(item_no), nopiece, line, date_piece)
            ht = line["qte"] * line["prix"]
            montant_ht += ht
            tva_tot += ht * line["tva"] / 100.0

        montant_ttc = montant_ht + tva_tot
        imp.update_totaux(nopiece, round(montant_ht, 4),
                          round(tva_tot, 4), round(montant_ttc, 4))
        # tenir les generateurs a jour (au cas ou l'appli les utilise)
        imp.advance_generator("NEXTPIECE", int(nopiece))
        imp.advance_generator("NEXTITEM", item_no)
        ref_piece = imp.get_ref_piece(nopiece)

        # 3) Resume
        print("-" * 60)
        print("Bon de reception : NOPIECE=%s  REF_PIECE=%s  (type %s)"
              % (nopiece, ref_piece, cfg["code_type_piece"]))
        print("Fournisseur : %s    Depot : %s"
              % (cfg.get("code_tiers") or "(aucun)", cfg.get("code_depot") or "(aucun)"))
        print("Articles crees   : %d  %s"
              % (len(created), created if len(created) <= 20 else created[:20] + ["..."]))
        print("Articles existants : %d" % len(existing))
        print("Articles mis a jour (prix achat) : %d" % len(updated))
        print("Codes-barres renseignes : %d" % len(imp.barcode_added))
        if imp.barcode_skipped:
            print("Codes-barres ignores (doublon/conflit) : %d  %s"
                  % (len(imp.barcode_skipped),
                     [b for _r, b in imp.barcode_skipped[:10]]))
        print("Total HT  : %.2f" % montant_ht)
        print("Total TVA : %.2f" % tva_tot)
        print("Total TTC : %.2f" % montant_ttc)

        if args.dry_run:
            con.rollback()
            print("\n[DRY-RUN] Aucune modification enregistree (rollback).")
        else:
            con.commit()
            print("\nImport termine et enregistre (commit).")
    except Exception as exc:
        con.rollback()
        msg = str(exc)
        print("\nECHEC : aucune ecriture (transaction annulee).", file=sys.stderr)
        print("Detail : %s" % msg, file=sys.stderr)
        if ("transliterate" in msg.lower() or "encod" in msg.lower()
                or "malformed" in msg.lower() or "character set" in msg.lower()):
            print("-> Probleme d'encodage. Le texte est pourtant assaini vers le "
                  "charset configure (%s). Verifiez la cle \"charset\" du config "
                  "(WIN1252 recommande pour ces bases ; sinon NONE)."
                  % cfg.get("charset"), file=sys.stderr)
        sys.exit(1)
    finally:
        con.close()


if __name__ == "__main__":
    main()
