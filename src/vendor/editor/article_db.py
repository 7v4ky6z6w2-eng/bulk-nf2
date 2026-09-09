# -*- coding: utf-8 -*-
"""
article_db.py
=============

Couche d'acces a la base Firebird PRIME (table ARTICLE) pour l'editeur en
masse. Tout le SQL est ici ; l'interface graphique ne parle qu'a cette classe.

Connexion : module ``fdb`` (le meme que votre script d'import), charset par
defaut WIN1252, configuration lue dans un ``config.json`` au format identique
a celui d'import_bon_reception.py.

Points importants
-----------------
* La cle d'une ligne est sa REFERENCE (REF_ART). Si l'utilisateur modifie la
  reference elle-meme, on met a jour en utilisant l'ANCIENNE valeur dans la
  clause WHERE (voir update_rows).
* Toutes les modifications d'un enregistrement sont faites dans UNE seule
  transaction : commit() ecrit tout, rollback() annule tout.
* Les tailles reelles des colonnes texte sont lues dans les tables systeme
  (RDB$) pour valider/tronquer correctement (en octets).
"""

from __future__ import annotations

import json
import os

from editor_logic import Cols, detect_columns

try:
    import fdb
except ImportError:                      # pragma: no cover - depend de l'env
    fdb = None


# --------------------------------------------------------------------------- #
#  Configuration
# --------------------------------------------------------------------------- #
DEFAULT_CONFIG = {
    "host": "localhost",          # "" ou null = acces local direct au fichier
    "port": 3050,
    "database": "C:\\PRIME\\PR22.FDB",
    "user": "SYSDBA",
    "password": "masterkey",
    "charset": "WIN1252",
    "table": "ARTICLE",
    "fb_client_library": "",      # ex: chemin de fbclient.dll si besoin
}


def _loads_tolerant(text):
    """json.loads, mais tolere les chemins Windows a simples antislashs.

    Sous Windows on colle facilement "C:\\PRIME\\PR22.FDB" tel quel dans le
    config.json, ce qui est un JSON invalide (\\P, \\b... ne sont pas des
    echappements reconnus). On double alors les antislashs qui ne font pas
    partie d'un echappement JSON valide, puis on reessaie.
    """
    import json as _json
    import re
    try:
        return _json.loads(text)
    except _json.JSONDecodeError:
        # Double tout antislash qui ne forme pas un echappement JSON "structurel"
        # (\\  \"  \/  \uXXXX). Dans un fichier de config de connexion, les
        # valeurs sont des chemins Windows : un \n / \t y est un separateur de
        # dossier litteral, pas un saut de ligne ou une tabulation.
        fixed = re.sub(r'\\(?!["\\/]|u[0-9a-fA-F]{4})', r'\\\\', text)
        return _json.loads(fixed)   # si ca echoue encore, l'erreur remonte


def load_config(path):
    """Charge la config JSON (et complete avec les valeurs par defaut)."""
    cfg = dict(DEFAULT_CONFIG)
    if path and os.path.isfile(path):
        with open(path, "r", encoding="utf-8") as fh:
            raw = fh.read()
        try:
            cfg.update(_loads_tolerant(raw))
        except json.JSONDecodeError as exc:
            raise DBError(
                "Le fichier de configuration '%s' n'est pas un JSON valide "
                "(ligne %d, colonne %d) : %s\n\n"
                "Astuce : dans un chemin Windows, ecrivez les antislashs en "
                "double (C:\\\\PRIME\\\\PR22.FDB) ou utilisez des slashs "
                "(C:/PRIME/PR22.FDB)." % (path, exc.lineno, exc.colno, exc.msg)
            ) from exc
    return cfg


def save_config(path, cfg):
    keys = list(DEFAULT_CONFIG)
    data = {k: cfg.get(k, DEFAULT_CONFIG[k]) for k in keys}
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, ensure_ascii=False)


_CHARSET_CODECS = {
    "UTF8": "utf-8", "UNICODE_FSS": "utf-8", "WIN1252": "cp1252",
    "WIN1256": "cp1256", "WIN1250": "cp1250", "WIN1251": "cp1251",
    "ISO8859_1": "iso-8859-1", "ISO8859_15": "iso-8859-15",
    "LATIN1": "iso-8859-1", "DOS850": "cp850", "ASCII": "ascii",
    "OCTETS": "latin-1", "NONE": "cp1252",
}


def python_codec(fb_charset):
    return _CHARSET_CODECS.get((fb_charset or "NONE").upper(), "cp1252")


# --------------------------------------------------------------------------- #
#  Erreurs
# --------------------------------------------------------------------------- #
class DBError(Exception):
    """Erreur metier remontee a l'interface (message lisible)."""


# --------------------------------------------------------------------------- #
#  Acces aux articles
# --------------------------------------------------------------------------- #
class ArticleRepository:
    """Encapsule la connexion et les operations sur la table ARTICLE."""

    def __init__(self, cfg):
        if fdb is None:
            raise DBError(
                "Le module 'fdb' n'est pas installe. Lancez :\n"
                "    pip install -r requirements.txt")
        self.cfg = cfg
        self.table = cfg.get("table", "ARTICLE")
        self.codec = python_codec(cfg.get("charset"))
        self.con = None
        self.columns = []        # noms reels des colonnes de la table
        self.colmap = {}         # logique -> reel
        self.maxlen = {}         # nom reel -> taille en octets (champs texte)
        self._pending = False    # une transaction est-elle en cours d'edition ?
        # referentiel des familles (None si la table n'existe pas)
        self.famille_table = None
        self.fam_code = None
        self.fam_name = None
        self.fam_tva = None
        self.fam_name_max = 50

    # -- connexion --------------------------------------------------------
    def connect(self):
        lib = self.cfg.get("fb_client_library")
        if lib:
            fdb.load_api(lib)
        kwargs = dict(database=self.cfg["database"], user=self.cfg["user"],
                      password=self.cfg["password"],
                      charset=self.cfg.get("charset", "WIN1252"))
        host = self.cfg.get("host")
        if host:
            kwargs["host"] = host
            if self.cfg.get("port"):
                kwargs["port"] = int(self.cfg["port"])
        try:
            self.con = fdb.connect(**kwargs)
        except Exception as exc:                       # noqa: BLE001
            raise DBError("Connexion impossible : %s" % exc) from exc
        self._introspect()
        return self

    def close(self):
        if self.con is not None:
            try:
                self.con.close()
            finally:
                self.con = None

    # -- introspection ----------------------------------------------------
    def _introspect(self):
        """Lit les colonnes reelles de la table et leur taille (octets)."""
        cur = self.con.cursor()
        cur.execute(
            "SELECT TRIM(rf.RDB$FIELD_NAME), f.RDB$FIELD_TYPE, f.RDB$FIELD_LENGTH "
            "FROM RDB$RELATION_FIELDS rf "
            "JOIN RDB$FIELDS f ON f.RDB$FIELD_NAME = rf.RDB$FIELD_SOURCE "
            "WHERE rf.RDB$RELATION_NAME = ? "
            "ORDER BY rf.RDB$FIELD_POSITION",
            (self.table.upper(),))
        rows = cur.fetchall()
        if not rows:
            raise DBError("Table '%s' introuvable dans la base." % self.table)
        self.columns = [r[0] for r in rows]
        # types texte Firebird : 14=CHAR, 37=VARCHAR, 261=BLOB
        for name, ftype, flen in rows:
            if ftype in (14, 37) and flen:
                self.maxlen[name] = int(flen)
        self.colmap = detect_columns(self.columns)
        # garde-fou : completer avec les tailles par defaut connues
        for logical, dflt in Cols.DEFAULT_MAX_LEN.items():
            real = self.colmap.get(logical)
            if real and real not in self.maxlen:
                self.maxlen[real] = dflt
        self._introspect_familles()
        self._introspect_tarifs()
        self._introspect_equiv()

    def _introspect_familles(self):
        """Detecte la table des familles et ses colonnes (code / nom / tva)."""
        table = self.cfg.get("famille_table", Cols.FAMILLE_TABLE)
        cur = self.con.cursor()
        try:
            cur.execute(
                "SELECT TRIM(rf.RDB$FIELD_NAME), f.RDB$FIELD_TYPE, f.RDB$FIELD_LENGTH "
                "FROM RDB$RELATION_FIELDS rf "
                "JOIN RDB$FIELDS f ON f.RDB$FIELD_NAME = rf.RDB$FIELD_SOURCE "
                "WHERE rf.RDB$RELATION_NAME = ? ORDER BY rf.RDB$FIELD_POSITION",
                (table.upper(),))
            rows = cur.fetchall()
        except Exception:                              # noqa: BLE001
            rows = []
        if not rows:
            return
        names = [r[0] for r in rows]
        sizes = {r[0]: int(r[2]) for r in rows if r[1] in (14, 37) and r[2]}

        def pick(*cands):
            for c in cands:
                if c in names:
                    return c
            return None

        self.fam_code = pick(Cols.FAMILLE_CODE, "CODE_FAMILLE", "CODE")
        self.fam_name = pick(Cols.FAMILLE_NAME, "LIBELLE", "NOM", "DESIGNATION")
        self.fam_tva = pick(Cols.FAMILLE_TVA, "TVA")
        if self.fam_code and self.fam_name:
            self.famille_table = table
            self.fam_name_max = sizes.get(self.fam_name, 50)

    # -- referentiel des familles ----------------------------------------
    def has_famille_ref(self):
        return self.famille_table is not None

    def list_familles(self):
        """Renvoie la liste [(code, intitule), ...] des familles existantes."""
        if not self.famille_table:
            return []
        cur = self.con.cursor()
        cur.execute("SELECT %s, %s FROM %s ORDER BY %s"
                    % (self.fam_code, self.fam_name, self.famille_table, self.fam_code))
        return [(r[0], r[1]) for r in cur.fetchall()]

    def famille_exists(self, code):
        if not self.famille_table or code in (None, ""):
            return False
        cur = self.con.cursor()
        cur.execute("SELECT 1 FROM %s WHERE %s = ?"
                    % (self.famille_table, self.fam_code), (code,))
        return cur.fetchone() is not None

    def _create_famille(self, cur, code, intitule, tva=None):
        """INSERT d'une famille (dans la transaction courante, sans commit)."""
        from editor_logic import fit_text, parse_number
        name, _ = fit_text(intitule or code, self.fam_name_max, self.codec)
        cols = [self.fam_code, self.fam_name]
        vals = [code, name]
        if self.fam_tva:
            cols.append(self.fam_tva)
            vals.append(parse_number(tva) if tva not in (None, "") else None)
        placeholders = ", ".join("?" for _ in cols)
        cur.execute("INSERT INTO %s (%s) VALUES (%s)"
                    % (self.famille_table, ", ".join(cols), placeholders), vals)

    def has(self, logical):
        return logical in self.colmap

    def real(self, logical):
        """Nom reel d'une colonne logique (ou None si absente)."""
        return self.colmap.get(logical)

    def display_columns(self):
        """Colonnes logiques effectivement presentes, dans l'ordre d'affichage."""
        return [c for c in Cols.DISPLAY if c in self.colmap]

    # -- lecture ----------------------------------------------------------
    def load(self, search="", limit=5000):
        """Charge les articles (eventuellement filtres) sous forme de dicts.

        Le filtre `search` est applique sur REF_ART, DESIGNATION et sur les
        CODES EQUIVALENTS (table EQUIV_CBARRES) — LIKE, insensible a la casse.
        Chaque dict contient les colonnes logiques presentes + "__ref0__"
        (valeur d'origine de la reference, qui sert de cle de mise a jour).
        """
        cols = self.display_columns()
        extra = [c for c in Cols.EXTRA_LOAD
                 if c in self.colmap and c not in cols]
        all_cols = cols + extra
        reals = [self.real(c) for c in all_cols]
        select = ", ".join(reals)
        sql = f"SELECT {select} FROM {self.table}"
        params = []
        if search:
            like = "%" + search.strip().upper() + "%"
            search_cols = [self.real(c) for c in (Cols.REF, Cols.DESIGNATION)
                           if self.has(c)]
            clauses = ["UPPER(%s) LIKE ?" % c for c in search_cols]
            params = [like] * len(search_cols)
            if self.has_equiv():
                clauses.append(
                    "EXISTS (SELECT 1 FROM %s e WHERE e.%s = %s.%s "
                    "AND UPPER(e.%s) LIKE ?)"
                    % (self.equiv_table, self.equiv_ref, self.table,
                       self.real(Cols.REF), self.equiv_code))
                params.append(like)
            sql += " WHERE " + " OR ".join(clauses)
        ref_real = self.real(Cols.REF)
        sql += f" ORDER BY {ref_real}"
        cur = self.con.cursor()
        try:
            cur.execute(sql, params)
            rows = cur.fetchmany(limit)
        except Exception as exc:                       # noqa: BLE001
            raise DBError("Lecture impossible : %s" % exc) from exc
        out = []
        for row in rows:
            rec = {}
            for logical, value in zip(all_cols, row):
                rec[logical] = value
            rec["__ref0__"] = rec.get(Cols.REF)
            out.append(rec)
        return out

    def count(self):
        cur = self.con.cursor()
        cur.execute(f"SELECT COUNT(*) FROM {self.table}")
        return cur.fetchone()[0]

    # -- ecriture ---------------------------------------------------------
    def update_rows(self, changes, new_familles=None):
        """Applique une liste de modifications (sans committer).

        `changes` : liste de dicts {
            "ref0": <reference d'origine, cle WHERE>,
            "values": { <colonne_logique>: <nouvelle valeur>, ... }
        }
        `new_familles` : liste optionnelle de (code, intitule, tva) a creer
        AVANT d'affecter les articles (meme transaction). Une famille deja
        presente est ignoree.

        Les valeurs texte sont tronquees a la taille de colonne ; les valeurs
        numeriques sont converties. Renvoie le nombre de lignes modifiees.
        Leve DBError en cas d'echec (et laisse la transaction telle quelle pour
        un rollback par l'appelant).
        """
        from editor_logic import parse_number, fit_text, parse_bool, parse_date

        cur = self.con.cursor()
        # 1) creer les familles manquantes (avant les articles -> FK satisfaite)
        for code, intitule, tva in (new_familles or []):
            try:
                if not self.famille_exists(code):
                    self._create_famille(cur, code, intitule, tva)
            except Exception as exc:                   # noqa: BLE001
                raise DBError("Creation de la famille '%s' impossible : %s"
                              % (code, exc)) from exc
        n = 0
        ref_real = self.real(Cols.REF)
        for change in changes:
            values = change.get("values") or {}
            if not values:
                continue
            set_parts, params = [], []
            for logical, val in values.items():
                real = self.real(logical)
                if real is None:
                    continue
                if logical in Cols.INT_FIELDS:
                    b = parse_bool(val) if logical in Cols.BOOL_FIELDS else None
                    if b is not None:
                        params.append(b)
                    else:
                        num = parse_number(val)
                        params.append(int(num) if num is not None else None)
                elif logical in Cols.DATE_FIELDS:
                    params.append(parse_date(val))
                elif logical in Cols.NUMERIC:
                    params.append(parse_number(val))
                else:
                    maxb = self.maxlen.get(real, Cols.DEFAULT_MAX_LEN.get(logical, 255))
                    fitted, _ = fit_text(val, maxb, self.codec)
                    if fitted == "":
                        fitted = None
                    params.append(fitted)
                set_parts.append(f"{real} = ?")
            if not set_parts:
                continue
            sql = f"UPDATE {self.table} SET {', '.join(set_parts)} WHERE {ref_real} = ?"
            params.append(change["ref0"])
            try:
                cur.execute(sql, params)
                n += cur.rowcount if cur.rowcount and cur.rowcount > 0 else 1
            except Exception as exc:                   # noqa: BLE001
                raise DBError(
                    "Echec sur la reference '%s' : %s" % (change.get("ref0"), exc)
                ) from exc
        self._pending = True
        return n

    def ref_exists(self, ref):
        cur = self.con.cursor()
        cur.execute(
            f"SELECT 1 FROM {self.table} WHERE {self.real(Cols.REF)} = ?", (ref,))
        return cur.fetchone() is not None

    def commit(self):
        if self.con is not None:
            self.con.commit()
            self._pending = False

    def rollback(self):
        if self.con is not None:
            self.con.rollback()
            self._pending = False

    # -- tarifs -----------------------------------------------------------
    def _introspect_tarifs(self):
        """Detecte les tables TYPE_TARIF (types) et TARIF (prix par article).

        Dans PRIME : TYPE_TARIF(CODE_TYPE_TARIF, INTITULE=nom T1/T2..., MARGE)
        et TARIF(CODE_TARIF=PK, CODE_TYPE_TARIF=FK, REF_ART=FK, PRIXHT=prix...).
        Le NOM affiche du tarif est INTITULE ; le PRIX par article est PRIXHT.
        On detecte les vraies colonnes pour rester robuste si elles different.
        """
        self.tarif_table = None
        self.tarif_type_table = None
        self.tarif_price_col = None
        self.tarif_pk = None
        self.tarif_name_col = None
        self.tarif_gen = None
        self._tt_names = None
        cur = self.con.cursor()

        def cols(tbl):
            try:
                cur.execute(
                    "SELECT TRIM(rf.RDB$FIELD_NAME) FROM RDB$RELATION_FIELDS rf "
                    "WHERE rf.RDB$RELATION_NAME = ? ORDER BY rf.RDB$FIELD_POSITION",
                    (tbl,))
                return [r[0] for r in cur.fetchall()]
            except Exception:                          # noqa: BLE001
                return []

        tt = cols("TYPE_TARIF")
        tf = cols("TARIF")
        if not tt or not tf:
            return
        if "REF_ART" not in tf or "CODE_TYPE_TARIF" not in tf:
            return
        # type de tarif (nom = INTITULE)
        self.tarif_type_table = "TYPE_TARIF"
        self.tarif_type_code = "CODE_TYPE_TARIF" if "CODE_TYPE_TARIF" in tt else tt[0]
        self.tarif_type_name = "INTITULE" if "INTITULE" in tt else self.tarif_type_code
        # table des prix
        self.tarif_table = "TARIF"
        self.tarif_ref = "REF_ART"
        self.tarif_type_fk = "CODE_TYPE_TARIF"
        for cand in ("PRIXHT", "TARIF_P_QTE", "PRIX", "PRIXVENTEHT", "MONTANT"):
            if cand in tf:
                self.tarif_price_col = cand
                break
        self.tarif_pk = "CODE_TARIF" if "CODE_TARIF" in tf else None
        self.tarif_name_col = "INTITULE" if "INTITULE" in tf else None
        # generateur du PK (NEXTTARIF dans PRIME)
        try:
            cur.execute("SELECT TRIM(RDB$GENERATOR_NAME) FROM RDB$GENERATORS "
                        "WHERE RDB$GENERATOR_NAME CONTAINING 'TARIF'")
            gens = [r[0] for r in cur.fetchall()]
            self.tarif_gen = "NEXTTARIF" if "NEXTTARIF" in gens else (gens[0] if gens else None)
        except Exception:                              # noqa: BLE001
            self.tarif_gen = None

    def has_tarifs(self):
        return getattr(self, "tarif_table", None) is not None and self.tarif_price_col

    @staticmethod
    def _clean(v):
        return v.strip() if isinstance(v, str) else v

    def load_tarif_types(self):
        """Renvoie [(code, nom), ...] des types de tarif (nom = INTITULE).

        On ne garde que les types reellement utilises (au moins une ligne TARIF
        avec un prix), ce qui ecarte les types speciaux (QTE, COND...) sans prix
        unitaire. Repli : tous les types si la sonde echoue.
        """
        if not getattr(self, "tarif_type_table", None):
            return []
        cur = self.con.cursor()
        used = None
        if self.has_tarifs():
            try:
                cur.execute("SELECT DISTINCT %s FROM %s WHERE %s IS NOT NULL"
                            % (self.tarif_type_fk, self.tarif_table, self.tarif_price_col))
                used = {self._clean(r[0]) for r in cur.fetchall()}
            except Exception:                          # noqa: BLE001
                used = None
        cur.execute("SELECT %s, %s FROM %s ORDER BY %s"
                    % (self.tarif_type_code, self.tarif_type_name,
                       self.tarif_type_table, self.tarif_type_code))
        out = []
        for code, name in cur.fetchall():
            code = self._clean(code)
            name = self._clean(name)
            if used is not None and code not in used:
                continue
            out.append((code, name or str(code)))
        return out

    def _type_names(self):
        if self._tt_names is None:
            self._tt_names = {c: n for c, n in self.load_tarif_types()}
        return self._tt_names

    def load_tarifs(self, refs):
        """Renvoie {ref: {type_code: prix}} depuis la table TARIF (PRIXHT)."""
        if not self.has_tarifs() or not refs:
            return {}
        refs = [r for r in refs if r is not None]
        cur = self.con.cursor()
        result = {}
        CHUNK = 200
        for i in range(0, len(refs), CHUNK):
            chunk = refs[i:i + CHUNK]
            placeholders = ",".join("?" for _ in chunk)
            cur.execute(
                "SELECT %s, %s, %s FROM %s WHERE %s IN (%s)"
                % (self.tarif_ref, self.tarif_type_fk, self.tarif_price_col,
                   self.tarif_table, self.tarif_ref, placeholders),
                chunk)
            for ref, type_code, price in cur.fetchall():
                # NE PAS nettoyer 'ref' : il doit rester identique a REF_ART de la
                # ligne article (__ref0__) pour que la grille retrouve le tarif.
                result.setdefault(ref, {})[self._clean(type_code)] = price
        return result

    def _next_tarif_id(self, cur):
        """Nouvel identifiant de tarif (CODE_TARIF). VARCHAR -> renvoie une chaine."""
        if self.tarif_gen:
            cur.execute("SELECT GEN_ID(%s, 1) FROM RDB$DATABASE" % self.tarif_gen)
            return str(cur.fetchone()[0])
        cur.execute("SELECT MAX(CAST(%s AS BIGINT)) FROM %s"
                    % (self.tarif_pk, self.tarif_table))
        row = cur.fetchone()
        return str((row[0] or 0) + 1)

    def update_tarifs(self, tarif_changes):
        """Ecrit les prix de tarif (sans committer).

        tarif_changes : liste de (ref0, type_code, prix).
        UPDATE du PRIX si la ligne (type, ref) existe ; INSERT sinon
        (CODE_TARIF via le generateur). prix=None -> suppression de la ligne.
        """
        from editor_logic import parse_number
        if not self.has_tarifs():
            raise DBError("Table des tarifs (TARIF) introuvable dans la base.")
        cur = self.con.cursor()
        names = self._type_names()
        for ref, type_code, price in tarif_changes:
            cur.execute(
                "SELECT 1 FROM %s WHERE %s = ? AND %s = ?"
                % (self.tarif_table, self.tarif_type_fk, self.tarif_ref),
                (type_code, ref))
            existing = cur.fetchone()
            pv = parse_number(price)
            try:
                if existing is not None:
                    if pv is None:
                        cur.execute(
                            "DELETE FROM %s WHERE %s = ? AND %s = ?"
                            % (self.tarif_table, self.tarif_type_fk, self.tarif_ref),
                            (type_code, ref))
                    else:
                        cur.execute(
                            "UPDATE %s SET %s = ? WHERE %s = ? AND %s = ?"
                            % (self.tarif_table, self.tarif_price_col,
                               self.tarif_type_fk, self.tarif_ref),
                            (pv, type_code, ref))
                elif pv is not None:
                    cols_ins = [self.tarif_type_fk, self.tarif_ref, self.tarif_price_col]
                    vals = [type_code, ref, pv]
                    if self.tarif_name_col:
                        cols_ins.append(self.tarif_name_col)
                        vals.append(names.get(type_code, type_code))
                    if self.tarif_pk:
                        cols_ins.insert(0, self.tarif_pk)
                        vals.insert(0, self._next_tarif_id(cur))
                    placeholders = ",".join("?" for _ in cols_ins)
                    cur.execute("INSERT INTO %s (%s) VALUES (%s)"
                                % (self.tarif_table, ",".join(cols_ins), placeholders),
                                vals)
            except Exception as exc:                   # noqa: BLE001
                raise DBError(
                    "Echec tarif '%s' / ref '%s' : %s" % (type_code, ref, exc)
                ) from exc
        self._pending = True

    # -- codes equivalents (plusieurs codes-barres par article) -----------
    def _introspect_equiv(self):
        """Detecte la table des codes equivalents (EQUIV_CBARRES dans PRIME).

        Schema attendu : NOEQUIV_CBARRES (PK, generateur NEXTEQUIV_CBARRES),
        REF_ART (FK vers ARTICLE), CODE_BARRES (le code equivalent, 60 c.).
        """
        self.equiv_table = None
        self.equiv_pk = None
        self.equiv_ref = None
        self.equiv_code = None
        self.equiv_gen = None
        self.equiv_code_max = Cols.EQUIV_CODE_LEN
        cur = self.con.cursor()
        try:
            cur.execute(
                "SELECT TRIM(rf.RDB$FIELD_NAME), f.RDB$FIELD_TYPE, f.RDB$FIELD_LENGTH "
                "FROM RDB$RELATION_FIELDS rf "
                "JOIN RDB$FIELDS f ON f.RDB$FIELD_NAME = rf.RDB$FIELD_SOURCE "
                "WHERE rf.RDB$RELATION_NAME = ? ORDER BY rf.RDB$FIELD_POSITION",
                (Cols.EQUIV_TABLE,))
            rows = cur.fetchall()
        except Exception:                              # noqa: BLE001
            rows = []
        if not rows:
            return
        names = [r[0] for r in rows]
        if Cols.EQUIV_REF not in names or Cols.EQUIV_CODE not in names:
            return
        self.equiv_table = Cols.EQUIV_TABLE
        self.equiv_ref = Cols.EQUIV_REF
        self.equiv_code = Cols.EQUIV_CODE
        self.equiv_pk = Cols.EQUIV_PK if Cols.EQUIV_PK in names else None
        for name, ftype, flen in rows:
            if name == self.equiv_code and ftype in (14, 37) and flen:
                self.equiv_code_max = int(flen)
        try:
            cur.execute("SELECT TRIM(RDB$GENERATOR_NAME) FROM RDB$GENERATORS "
                        "WHERE RDB$GENERATOR_NAME CONTAINING 'EQUIV'")
            gens = [r[0] for r in cur.fetchall()]
            self.equiv_gen = (Cols.EQUIV_GEN if Cols.EQUIV_GEN in gens
                              else (gens[0] if gens else None))
        except Exception:                              # noqa: BLE001
            self.equiv_gen = None

    def has_equiv(self):
        return getattr(self, "equiv_table", None) is not None

    def load_equiv(self, refs):
        """Renvoie {ref: [code, ...]} depuis la table EQUIV_CBARRES."""
        if not self.has_equiv() or not refs:
            return {}
        refs = [r for r in refs if r is not None]
        order = self.equiv_pk or self.equiv_code
        cur = self.con.cursor()
        result = {}
        CHUNK = 200
        for i in range(0, len(refs), CHUNK):
            chunk = refs[i:i + CHUNK]
            placeholders = ",".join("?" for _ in chunk)
            cur.execute(
                "SELECT %s, %s FROM %s WHERE %s IN (%s) ORDER BY %s"
                % (self.equiv_ref, self.equiv_code, self.equiv_table,
                   self.equiv_ref, placeholders, order),
                chunk)
            for ref, code in cur.fetchall():
                # NE PAS nettoyer 'ref' : il doit rester identique a REF_ART
                # de la ligne article (__ref0__).
                if code is None:
                    continue
                code = self._clean(code)
                if code:
                    result.setdefault(ref, []).append(code)
        return result

    def _next_equiv_id(self, cur):
        """Nouvel identifiant (NOEQUIV_CBARRES). VARCHAR -> renvoie une chaine."""
        if self.equiv_gen:
            cur.execute("SELECT GEN_ID(%s, 1) FROM RDB$DATABASE" % self.equiv_gen)
            return str(cur.fetchone()[0])
        cur.execute("SELECT MAX(CAST(%s AS BIGINT)) FROM %s"
                    % (self.equiv_pk, self.equiv_table))
        row = cur.fetchone()
        return str((row[0] or 0) + 1)

    def update_equiv(self, equiv_changes):
        """Remplace les codes equivalents d'articles (sans committer).

        equiv_changes : liste de (ref0, [codes]). La liste fournie REMPLACE
        integralement les codes existants de l'article (liste vide = tout
        supprimer). Codes tronques a la taille de la colonne.
        """
        from editor_logic import fit_text
        if not self.has_equiv():
            raise DBError("Table des codes equivalents (EQUIV_CBARRES) "
                          "introuvable dans la base.")
        cur = self.con.cursor()
        for ref, codes in equiv_changes:
            try:
                cur.execute("DELETE FROM %s WHERE %s = ?"
                            % (self.equiv_table, self.equiv_ref), (ref,))
                for code in (codes or []):
                    code, _ = fit_text(code, self.equiv_code_max, self.codec)
                    if not code:
                        continue
                    cols_ins = [self.equiv_ref, self.equiv_code]
                    vals = [ref, code]
                    if self.equiv_pk:
                        cols_ins.insert(0, self.equiv_pk)
                        vals.insert(0, self._next_equiv_id(cur))
                    placeholders = ",".join("?" for _ in cols_ins)
                    cur.execute("INSERT INTO %s (%s) VALUES (%s)"
                                % (self.equiv_table, ",".join(cols_ins),
                                   placeholders), vals)
            except Exception as exc:                   # noqa: BLE001
                raise DBError(
                    "Echec codes equivalents ref '%s' : %s" % (ref, exc)
                ) from exc
        self._pending = True


# --------------------------------------------------------------------------- #
#  Depot de DEMONSTRATION (sans Firebird) — pour tester l'interface
# --------------------------------------------------------------------------- #
class DemoRepository:
    """Imite ArticleRepository avec des donnees en memoire (mode --demo).

    Permet de lancer et tester l'interface sans serveur Firebird. Le commit
    ne fait qu'entreriner les changements dans la liste en memoire.
    """

    def __init__(self, cfg=None):
        self.cfg = cfg or {}
        self.table = "ARTICLE (demo)"
        self.codec = "cp1252"
        self.columns = list(Cols.DISPLAY)
        # colonnes "possedees" : celles affichees + les colonnes promo non
        # affichees (HT promo, dates) pour coller au schema PRIME reel.
        self.colmap = {c: c for c in Cols.DISPLAY}
        for c in (Cols.PV_HT_PROMO, Cols.PROMO_START, Cols.PROMO_END):
            self.colmap[c] = c
        self.maxlen = {Cols.REF: 35, Cols.DESIGNATION: 100}
        self.famille_table = "FAMILLE (demo)"
        self.fam_code = Cols.FAMILLE_CODE
        self.fam_name = Cols.FAMILLE_NAME
        self.fam_tva = Cols.FAMILLE_TVA
        self.fam_name_max = 50
        self._familles = {"BOISSON": "Boissons", "EPICERIE": "Epicerie",
                          "HYGIENE": "Hygiene", "PAPETERIE": "Papeterie"}
        # types de tarif (code -> nom T1..T4), comme dans PRIME
        self._tarif_types = [("6", "T1"), ("7", "T2"), ("8", "T3"), ("9", "T4")]
        # prix par article : ref -> {code_type: prix}
        self._tarifs = {
            "A001": {"6": 2.20, "7": 2.40, "8": 2.60, "9": 2.80},
            "A002": {"6": 2.80, "7": 3.00},
            "A004": {"6": 7.00, "7": 7.50, "8": 8.20},
        }
        # codes equivalents : ref -> [codes] (plusieurs codes par article)
        self._equiv = {
            "A001": ["3001234500017", "3001234500918"],
            "A002": ["3001234500024"],
            "A004": ["3001234500048"],
            "A005": ["3001234500055"],
            "B011": ["3001234500079"],
        }
        self._rows = self._sample()
        self._staged = None

    def _sample(self):
        # colonnes : REF, DESIGNATION, PV_HT, PV_TTC, PV_TTC_PROMO,
        #            PROMO_ACTIVE, TVA, PA_HT, QTE_CARTON, FAMILLE
        data = [
            ("A001", "Cafe moulu 250g", 2.50, 2.98, 2.50, 1, 19, 1.40, 12, "BOISSON"),
            ("A002", "The vert bio 100g", 3.10, 3.69, None, 0, 19, 1.80, 12, "BOISSON"),
            ("A003", "Sucre blanc 1kg", 1.05, 1.25, None, 0, 19, 0.70, 10, "EPICERIE"),
            ("A004", "Huile olive 1L", 7.90, 9.40, 6.90, 1, 19, 5.20, 6, "EPICERIE"),
            ("A005", "Savon de Marseille", 1.80, 2.14, None, 0, 19, 0.95, 24, "HYGIENE"),
            ("B010", "Stylo bille bleu", 0.50, 0.60, None, 0, 19, 0.20, 50, "PAPETERIE"),
            ("B011", "Cahier 96 pages", 1.20, 1.43, None, 0, 19, 0.65, 25, "PAPETERIE"),
        ]
        rows = []
        for rec in data:
            d = dict(zip(Cols.DISPLAY, rec))
            d["__ref0__"] = d[Cols.REF]
            rows.append(d)
        return rows

    def connect(self):
        return self

    def close(self):
        pass

    def has(self, logical):
        return logical in self.colmap

    def real(self, logical):
        return self.colmap.get(logical)

    def display_columns(self):
        return list(Cols.DISPLAY)

    def count(self):
        return len(self._rows)

    def load(self, search="", limit=5000):
        rows = [dict(r) for r in self._rows]
        if search:
            s = search.strip().lower()
            rows = [r for r in rows
                    if any(s in str(r.get(c, "")).lower()
                           for c in (Cols.REF, Cols.DESIGNATION))
                    or any(s in code.lower()
                           for code in self._equiv.get(r.get(Cols.REF), []))]
        return rows[:limit]

    def ref_exists(self, ref):
        return any(r[Cols.REF] == ref for r in self._rows)

    def has_famille_ref(self):
        return True

    def list_familles(self):
        return sorted(self._familles.items())

    def famille_exists(self, code):
        return code in self._familles

    def update_rows(self, changes, new_familles=None):
        from editor_logic import parse_number, parse_bool, parse_date
        for code, intitule, _tva in (new_familles or []):
            if code and code not in self._familles:
                self._familles[code] = intitule or code
        staged = {r["__ref0__"]: dict(r) for r in self._rows}
        n = 0
        for change in changes:
            row = staged.get(change["ref0"])
            if not row:
                continue
            for logical, val in (change.get("values") or {}).items():
                if logical in Cols.INT_FIELDS:
                    b = parse_bool(val) if logical in Cols.BOOL_FIELDS else None
                    row[logical] = b if b is not None else (
                        int(parse_number(val)) if parse_number(val) is not None else None)
                elif logical in Cols.DATE_FIELDS:
                    row[logical] = parse_date(val)
                elif logical in Cols.NUMERIC:
                    row[logical] = parse_number(val)
                else:
                    row[logical] = val or None
            n += 1
        self._staged = list(staged.values())
        return n

    def commit(self):
        if self._staged is not None:
            for r in self._staged:
                r["__ref0__"] = r[Cols.REF]
            self._rows = self._staged
            self._staged = None

    def rollback(self):
        self._staged = None

    def has_tarifs(self):
        return True

    def load_tarif_types(self):
        return list(self._tarif_types)

    def load_tarifs(self, refs):
        refs = set(refs or [])
        return {r: dict(v) for r, v in self._tarifs.items() if r in refs}

    def update_tarifs(self, tarif_changes):
        from editor_logic import parse_number
        for ref, type_code, price in tarif_changes:
            pv = parse_number(price)
            slot = self._tarifs.setdefault(ref, {})
            if pv is None:
                slot.pop(type_code, None)
            else:
                slot[type_code] = pv

    def has_equiv(self):
        return True

    def load_equiv(self, refs):
        refs = set(refs or [])
        return {r: list(v) for r, v in self._equiv.items() if r in refs}

    def update_equiv(self, equiv_changes):
        for ref, codes in equiv_changes:
            codes = [c for c in (codes or []) if c]
            if codes:
                self._equiv[ref] = list(codes)
            else:
                self._equiv.pop(ref, None)
