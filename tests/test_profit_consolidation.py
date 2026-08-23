import copy
import os
import tempfile

from app.config import DEFAULT_CONFIG
from app.sync import profit_consolidation as pc
from app.sync.state_store import StateStore


class FakeCursor:
    def __init__(self, responder):
        self.responder = responder
        self.executed = []
        self._last = None

    def execute(self, sql, params=None):
        self.executed.append((sql, params))
        self._last = self.responder(sql, params)

    def fetchone(self):
        if isinstance(self._last, list):
            return self._last[0] if self._last else None
        return self._last

    def fetchall(self):
        return self._last if isinstance(self._last, list) else []


class FakeConnection:
    def __init__(self, cur):
        self._cur = cur
        self.committed = False

    def cursor(self):
        return self._cur

    def commit(self):
        self.committed = True

    def close(self):
        pass


def _cfg(state_path, **overrides):
    cfg = copy.deepcopy(DEFAULT_CONFIG)
    cfg["state_db_path"] = state_path
    cfg["order_import"]["client_code"] = "CLI-LIVRAISON"
    cfg["order_import"]["code_depot"] = "DP00001"
    cfg["order_import"].update(overrides)
    return cfg


def _responder(piece_types=None, item_rows=None, articles=None, target_client="Client Cible",
               max_nopiece=1000, max_noitem=5000, gen_piece=0, gen_item=0):
    """piece_types: list of (code_type_piece, intitule, nopiece, montant, annulee)
    item_rows: list of (nopiece, datepiece, p_annulee, ref_art, qte, prixht, prixttc, i_annulee)
    articles: {ref_art: (prixachatht, prixachattc, tva)}
    """
    piece_types = piece_types or []
    item_rows = item_rows or []
    articles = articles or {}

    def responder(sql, params):
        s = sql.upper()
        if "P.CODE_TYPE_PIECE, T.INTITULE, P.NOPIECE, P.MONTANT, P.ANNULEE" in s:
            return list(piece_types)
        if "P.NOPIECE, P.DATEPIECE, P.ANNULEE, I.REF_ART, I.QTE" in s:
            return list(item_rows)
        if "PRIXACHATHT, PRIXACHATTTC, TAUX_TVA FROM ARTICLE" in s:
            return [(ref, *articles[ref]) for ref in params if ref in articles]
        if "RAISON_SOCIALE FROM TIERS" in s:
            (code,) = params
            return (target_client,) if code == "TARGET" else None
        if "GEN_ID(NEXTPIECE" in s:
            return (gen_piece,)
        if "GEN_ID(NEXTITEM" in s:
            return (gen_item,)
        if "MAX(CAST(NOPIECE" in s:
            return (max_nopiece,)
        if "MAX(CAST(NOITEM" in s:
            return (max_noitem,)
        return None

    return responder


def _connect(monkeypatch, responder):
    cur = FakeCursor(responder)
    con = FakeConnection(cur)
    monkeypatch.setattr(pc, "connect_firebird", lambda cfg: con)
    return con, cur


def test_list_source_document_types_groups_by_type_and_flags_consolidated(monkeypatch):
    with tempfile.TemporaryDirectory() as tmp:
        state_path = os.path.join(tmp, "state.sqlite3")
        with StateStore(state_path) as store:
            store.mark_consolidated(["1"], "PC_VE_B", "999", "2026-01-01T00:00:00")

        responder = _responder(piece_types=[
            ("PC_VE_B", "Bon de Livraison", "1", 100.0, 1),
            ("PC_VE_B", "Bon de Livraison", "2", 50.0, 1),
            ("PC_VE_COM", "Commande", "3", 100.0, 1),
            ("PC_VE_B", "Bon de Livraison", "4", 10.0, 0),  # cancelled -- excluded
        ])
        _connect(monkeypatch, responder)

        result = pc.list_source_document_types(_cfg(state_path), "CLI-LIVRAISON")
        by_type = {e["code_type_piece"]: e for e in result}
        assert by_type["PC_VE_B"]["count"] == 2
        assert by_type["PC_VE_B"]["already_consolidated"] == 1
        assert by_type["PC_VE_B"]["total_montant"] == 150.0
        assert by_type["PC_VE_COM"]["count"] == 1


def test_preview_separates_ready_from_held_back_and_writes_nothing(monkeypatch):
    with tempfile.TemporaryDirectory() as tmp:
        state_path = os.path.join(tmp, "state.sqlite3")
        responder = _responder(
            item_rows=[
                ("1", "2026-01-01", 1, "REF1", 2.0, 24.0, 28.0, 1),
                ("2", "2026-01-02", 1, "REF2", 1.0, 10.0, 12.0, 1),  # missing from ARTICLE
            ],
            articles={"REF1": (20.0, 24.0, 19.0)},
        )
        con, cur = _connect(monkeypatch, responder)

        report = pc.preview_consolidation(_cfg(state_path), "CLI-LIVRAISON", "PC_VE_B")
        assert report["created_nopiece"] is None
        assert report["source_doc_count"] == 1
        assert report["line_count"] == 1
        assert report["total_cost_ht"] == 40.0  # REF1: 20.0 * 2
        assert len(report["held_back"]) == 1
        assert report["held_back"][0]["nopiece"] == "2"
        assert report["held_back"][0]["flags"] == [{"ref_art": "REF2", "reason": "missing_ref"}]
        assert not any("INSERT" in sql.upper() for sql, _ in cur.executed)
        assert con.committed is False


def test_zero_cost_article_is_included_at_zero_cost_and_flagged_as_warning(monkeypatch):
    # An article that DOES exist but has PRIXACHAT=0 must not hold back the
    # whole document -- included at 0 cost, just surfaced as a warning so
    # the user can fix the article's cost later (their explicit choice).
    with tempfile.TemporaryDirectory() as tmp:
        state_path = os.path.join(tmp, "state.sqlite3")
        responder = _responder(
            item_rows=[("1", "2026-01-01", 1, "REF1", 2.0, 24.0, 28.0, 1)],
            articles={"REF1": (0.0, 0.0, 19.0)},  # PRIXACHAT = 0
        )
        _connect(monkeypatch, responder)

        report = pc.preview_consolidation(_cfg(state_path), "CLI-LIVRAISON", "PC_VE_B")
        assert report["held_back"] == []
        assert report["line_count"] == 1
        assert report["total_cost_ht"] == 0.0
        assert report["total_cost_ttc"] == 0.0
        assert report["zero_cost_warnings"] == [{"ref_art": "REF1", "nopiece": "1"}]


def test_missing_ref_still_holds_back_its_document(monkeypatch):
    with tempfile.TemporaryDirectory() as tmp:
        state_path = os.path.join(tmp, "state.sqlite3")
        responder = _responder(
            item_rows=[("1", "2026-01-01", 1, "REF1", 1.0, 24.0, 28.0, 1)],
            articles={},  # REF1 doesn't exist in ARTICLE at all
        )
        _connect(monkeypatch, responder)

        report = pc.preview_consolidation(_cfg(state_path), "CLI-LIVRAISON", "PC_VE_B")
        assert report["line_count"] == 0
        assert report["held_back"][0]["flags"] == [{"ref_art": "REF1", "reason": "missing_ref"}]
        assert report["zero_cost_warnings"] == []


def test_run_consolidation_creates_one_doc_with_aggregated_lines_at_cost(monkeypatch):
    with tempfile.TemporaryDirectory() as tmp:
        state_path = os.path.join(tmp, "state.sqlite3")
        responder = _responder(
            item_rows=[
                ("1", "2026-01-01", 1, "REF1", 2.0, 24.0, 28.0, 1),
                ("2", "2026-01-02", 1, "REF1", 3.0, 24.0, 28.0, 1),  # same ref -- must aggregate
            ],
            articles={"REF1": (20.0, 24.0, 19.0)},
            max_nopiece=100, max_noitem=500,
        )
        con, cur = _connect(monkeypatch, responder)

        report = pc.run_consolidation(_cfg(state_path), "CLI-LIVRAISON", "PC_VE_B", "TARGET")
        assert report["created_nopiece"] == "101"
        assert report["source_doc_count"] == 2
        assert report["line_count"] == 1  # aggregated into one REF1 line
        assert report["total_cost_ht"] == 100.0  # 20.0 * (2 + 3)
        assert report["original_sale_ht"] == 120.0  # 24.0 * 5
        assert report["estimated_profit_ht"] == 20.0
        assert con.committed is True

        insert_item_sql, item_params = next(
            (sql, p) for sql, p in cur.executed if sql.upper().startswith("INSERT INTO ITEM")
        )
        assert item_params[3] == 5.0  # QTE aggregated
        # COEFF/COEFF_TR are hardcoded literal 0s in the SQL text itself
        # (not parameterized) -- see the module's correctness note.
        assert "COEFF, COEFF_TR" in insert_item_sql
        assert "0, 0" in insert_item_sql

        insert_piece_sql, piece_params = next(
            (sql, p) for sql, p in cur.executed if sql.upper().startswith("INSERT INTO PIECE")
        )
        assert "COEFF, COEFF_TR" in insert_piece_sql
        assert "0, 0" in insert_piece_sql
        assert piece_params[2] == "TARGET"  # CODE_TIERS = chosen client, not the source one


def test_run_consolidation_marks_source_docs_consolidated(monkeypatch):
    with tempfile.TemporaryDirectory() as tmp:
        state_path = os.path.join(tmp, "state.sqlite3")
        responder = _responder(
            item_rows=[("1", "2026-01-01", 1, "REF1", 1.0, 24.0, 28.0, 1)],
            articles={"REF1": (20.0, 24.0, 19.0)},
        )
        _connect(monkeypatch, responder)

        pc.run_consolidation(_cfg(state_path), "CLI-LIVRAISON", "PC_VE_B", "TARGET")

        with StateStore(state_path) as store:
            assert store.get_consolidated_source_nopieces() == {"1"}


def test_rerun_after_consolidation_only_picks_up_new_deliveries(monkeypatch):
    with tempfile.TemporaryDirectory() as tmp:
        state_path = os.path.join(tmp, "state.sqlite3")
        responder = _responder(
            item_rows=[("1", "2026-01-01", 1, "REF1", 1.0, 24.0, 28.0, 1)],
            articles={"REF1": (20.0, 24.0, 19.0)},
        )
        _connect(monkeypatch, responder)
        report1 = pc.run_consolidation(_cfg(state_path), "CLI-LIVRAISON", "PC_VE_B", "TARGET")
        assert report1["source_doc_count"] == 1

        # Same source data queried again (as a real re-run would) -- doc #1
        # is already consolidated, so nothing new should be picked up.
        report2 = pc.run_consolidation(_cfg(state_path), "CLI-LIVRAISON", "PC_VE_B", "TARGET")
        assert report2["source_doc_count"] == 0
        assert report2["already_consolidated_skipped"] == 1
        assert report2["created_nopiece"] is None


def test_held_back_document_is_not_marked_consolidated(monkeypatch):
    with tempfile.TemporaryDirectory() as tmp:
        state_path = os.path.join(tmp, "state.sqlite3")
        responder = _responder(
            item_rows=[("1", "2026-01-01", 1, "REFMISSING", 1.0, 24.0, 28.0, 1)],
            articles={},
        )
        _connect(monkeypatch, responder)

        report = pc.run_consolidation(_cfg(state_path), "CLI-LIVRAISON", "PC_VE_B", "TARGET")
        assert report["created_nopiece"] is None
        assert len(report["held_back"]) == 1

        with StateStore(state_path) as store:
            assert store.get_consolidated_source_nopieces() == set()


def test_partially_bad_document_holds_back_the_whole_document(monkeypatch):
    # One good line + one bad line in the SAME source document -- the good
    # line must NOT be split off into the consolidated doc on its own.
    with tempfile.TemporaryDirectory() as tmp:
        state_path = os.path.join(tmp, "state.sqlite3")
        responder = _responder(
            item_rows=[
                ("1", "2026-01-01", 1, "REF1", 1.0, 24.0, 28.0, 1),
                ("1", "2026-01-01", 1, "REFBAD", 1.0, 10.0, 12.0, 1),
            ],
            articles={"REF1": (20.0, 24.0, 19.0)},
        )
        _connect(monkeypatch, responder)

        report = pc.run_consolidation(_cfg(state_path), "CLI-LIVRAISON", "PC_VE_B", "TARGET")
        assert report["created_nopiece"] is None
        assert report["line_count"] == 0
        assert len(report["held_back"]) == 1


def test_run_consolidation_target_client_not_found_raises(monkeypatch):
    with tempfile.TemporaryDirectory() as tmp:
        state_path = os.path.join(tmp, "state.sqlite3")
        responder = _responder(item_rows=[])
        _connect(monkeypatch, responder)

        try:
            pc.run_consolidation(_cfg(state_path), "CLI-LIVRAISON", "PC_VE_B", "NO_SUCH_CLIENT")
            assert False, "expected RuntimeError"
        except RuntimeError as exc:
            assert "NO_SUCH_CLIENT" in str(exc)


def test_dry_run_flag_delegates_to_preview_and_writes_nothing(monkeypatch):
    with tempfile.TemporaryDirectory() as tmp:
        state_path = os.path.join(tmp, "state.sqlite3")
        responder = _responder(
            item_rows=[("1", "2026-01-01", 1, "REF1", 1.0, 24.0, 28.0, 1)],
            articles={"REF1": (20.0, 24.0, 19.0)},
        )
        con, cur = _connect(monkeypatch, responder)

        report = pc.run_consolidation(_cfg(state_path), "CLI-LIVRAISON", "PC_VE_B", "TARGET",
                                       dry_run=True)
        assert report["created_nopiece"] is None
        assert not any("INSERT" in sql.upper() for sql, _ in cur.executed)
        assert con.committed is False
