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
    return _smart_title(" ".join(expanded))


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
