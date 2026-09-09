"""Formatage des montants (style algérien/français : 12 500,00 DA)."""

from __future__ import annotations


def fmt_da(value, suffix: str = " DA") -> str:
    """12500 -> '12 500,00 DA' (espace = séparateur de milliers, virgule décimale)."""
    try:
        v = float(value or 0)
    except (TypeError, ValueError):
        return str(value)
    s = "{:,.2f}".format(v).replace(",", " ").replace(".", ",")
    return s + suffix


def fmt_qty(value) -> str:
    """Quantité : '12 500' si entière, sinon '12 500,50' (sans suffixe)."""
    try:
        v = float(value or 0)
    except (TypeError, ValueError):
        return str(value)
    if v == int(v):
        return "{:,.0f}".format(v).replace(",", " ")
    return "{:,.2f}".format(v).replace(",", " ").replace(".", ",")
