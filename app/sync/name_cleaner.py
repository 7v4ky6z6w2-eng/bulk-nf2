"""Product name cleaning: abbreviation expansion + French-aware Title Case.

Ported from the user's existing wc_create_new.py (NameCleaner), which they
already use in production against this same kind of ERP data. Raw
DESIGNATION values are often all-caps with abbreviations (e.g. "STYL BIL
BLU" for "Stylo Bille Bleu") -- this cleans them up before they become a
WooCommerce product name (and, via the category derivation in engine.py,
before the category name is derived too).
"""

import re

# French articles/connectors that stay lowercase in Title Case, except when
# they're the first word.
LOWERCASE_WORDS = {
    "de", "du", "des", "la", "le", "les", "l'",
    "à", "au", "aux", "et", "ou", "en", "pour", "sur",
    "avec", "sans", "par", "d", "l",
}


# Characters that never appear in real French words but commonly separate
# an internal reference/SKU code from a numeric part (e.g. "CL-2761/CRT1440",
# "V-1203", "REF:4995").
_CODE_SEPARATORS = set("-/:")


def clean_name(raw, replacements):
    """'replacements' is a dict of UPPERCASE token -> expansion, e.g.
    {"STYL": "Stylo", "GM": "Grand Modèle"}."""
    if not raw:
        return ""
    s = raw.replace(".", " ").replace(",", " ").replace("/", " / ")
    s = re.sub(r"\s+", " ", s).strip().upper()
    words = s.split()
    upper_replacements = {k.upper(): v for k, v in (replacements or {}).items()}
    expanded = [upper_replacements.get(w, w) for w in words]
    return strip_trailing_codes(_smart_title(" ".join(expanded)))


def _smart_title(s):
    parts = s.split()
    out = []
    for i, w in enumerate(parts):
        wl = w.lower()
        if i > 0 and wl in LOWERCASE_WORDS:
            out.append(wl)
        else:
            # Only re-case pure-alpha tokens; leave codes (e.g. "A4", "B/2") alone.
            out.append(w.capitalize() if w.isalpha() else w)
    return " ".join(out)


def _is_code_token(token):
    """True for tokens that are clearly internal reference/packaging codes
    rather than descriptive text -- e.g. "8448*576*24*" (packaging
    multiples), "TCHNO_9525_00", "CL-2761/CRT1440", "V-1203", "REF:4995".
    Deliberately conservative: plain numbers with no letters or separators
    (e.g. "20" in "BOITE DE 20") are NOT treated as codes, since real ERP
    data has meaningful trailing quantities like that."""
    digits = sum(c.isdigit() for c in token)
    if digits == 0:
        return False
    if "*" in token or "_" in token:
        return True
    if any(c in _CODE_SEPARATORS for c in token) and digits >= 3:
        return True
    letters = sum(c.isalpha() for c in token)
    if letters > 0 and token.isalnum() and digits >= 4:
        # Letters+digits glued together with no separator, e.g. "CRT1440".
        # Threshold is 4 (not 3) so legitimate specs like "120G" (grammage)
        # aren't mistaken for a code -- real internal codes in this data
        # consistently run 4+ digits.
        return True
    return False


def _is_bare_punctuation(token):
    return not any(c.isalnum() for c in token)


def strip_trailing_codes(name):
    """Drops trailing reference/packaging-code tokens (and any now-dangling
    bare punctuation left behind, e.g. a lone "/") from an already-cleaned
    name, so they don't show up in the customer-facing WooCommerce title.
    Always keeps at least one word."""
    words = name.split()
    while len(words) > 1 and (_is_code_token(words[-1]) or _is_bare_punctuation(words[-1])):
        words.pop()
    return " ".join(words)
