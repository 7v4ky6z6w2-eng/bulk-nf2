"""Shared Firebird PIECE/ITEM numbering + per-doc-type coefficient lookup.

Extracted from order_importer.py's OrderImporter so any other tool that
writes PIECE/ITEM rows (e.g. profit_consolidation.py) reuses the exact
same, already-proven generator-safety logic (real NEXTPIECE/NEXTITEM
generators, explicitly advanced after use) instead of a second hand-rolled
copy that could quietly drift and collide with NetFact2's own native
document numbering.
"""

import logging

log = logging.getLogger(__name__)

# Ignore reserved ID ranges (e.g. inventories) above this when computing
# MAX(existing) -- same threshold import_bon_reception.py uses.
RESERVED_ID_THRESHOLD = 1000000


class PieceWriter:
    def __init__(self, cur):
        self.cur = cur
        self._coeff_cache = {}

    def gen_value(self, generator):
        self.cur.execute(f"SELECT GEN_ID({generator}, 0) FROM RDB$DATABASE")
        return self.cur.fetchone()[0] or 0

    def advance_generator(self, generator, target):
        current = self.gen_value(generator)
        if target > current:
            self.cur.execute(f"SELECT GEN_ID({generator}, {target - current}) FROM RDB$DATABASE")
            self.cur.fetchone()

    def next_base(self, generator, table, col):
        self.cur.execute(
            f"SELECT MAX(CAST({col} AS BIGINT)) FROM {table} "
            f"WHERE {col} SIMILAR TO '[0-9]+' AND CHAR_LENGTH({col}) <= 15 "
            f"  AND CAST({col} AS BIGINT) < ?",
            (RESERVED_ID_THRESHOLD,),
        )
        mx = self.cur.fetchone()[0] or 0
        return max(int(mx), int(self.gen_value(generator)))

    def type_piece_coefficients(self, code_type_piece):
        """Returns (coeff_piece, coeff_piece_tr, coeff_item, coeff_item_tr)
        for this document type, cached per instance.

        NetFact2's TIERS balance is computed live as
        SUM(PIECE.MONTANT * PIECE.ANNULEE * (PIECE.COEFF + PIECE.COEFF_TR))
        -- confirmed from the application's own embedded SQL. PIECE.COEFF/
        COEFF_TR (and ITEM.COEFF/COEFF_TR, used the same way by the stock
        ledger) are per-document-type values NetFact2's own UI copies from
        LOCAL_TYPE_PIECE.COEFF_PIECE/COEFF_PIECE_TR/COEFF_ITEM/COEFF_ITEM_TR
        when creating a document -- leaving them at their column default
        (0) silently zeroes out both the balance and stock effect of a
        document regardless of ANNULEE. Looking this up live (instead of
        hardcoding an assumed sign) means it stays correct even if the
        coefficients differ per document type or change later, exactly
        mirroring what NetFact2 itself does on save.

        Falls back to (0, 0, 0, 0) with a warning if the type isn't found
        in LOCAL_TYPE_PIECE."""
        if code_type_piece in self._coeff_cache:
            return self._coeff_cache[code_type_piece]
        self.cur.execute(
            "SELECT COEFF_PIECE, COEFF_PIECE_TR, COEFF_ITEM, COEFF_ITEM_TR "
            "FROM LOCAL_TYPE_PIECE WHERE CODE_TYPE_PIECE = ?",
            (code_type_piece,),
        )
        row = self.cur.fetchone()
        if not row:
            log.warning("LOCAL_TYPE_PIECE has no row for CODE_TYPE_PIECE=%s -- "
                        "PIECE/ITEM COEFF will be 0, so this document won't affect "
                        "the client's balance or stock", code_type_piece)
            coeffs = (0, 0, 0, 0)
        else:
            coeffs = tuple(int(v) if v is not None else 0 for v in row)
        self._coeff_cache[code_type_piece] = coeffs
        return coeffs
