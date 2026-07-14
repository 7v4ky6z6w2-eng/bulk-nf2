import copy
from unittest.mock import patch, MagicMock

from app.config import DEFAULT_CONFIG
from app import diagnostics


def _cfg():
    cfg = copy.deepcopy(DEFAULT_CONFIG)
    cfg["woocommerce"]["site_url"] = "https://example.com"
    cfg["woocommerce"]["consumer_key"] = "ck"
    cfg["woocommerce"]["consumer_secret"] = "cs"
    return cfg


class FakeCursor:
    def execute(self, sql, params=None):
        pass

    def fetchone(self):
        return (2265,)


class FakeConnection:
    def cursor(self):
        return FakeCursor()

    def close(self):
        pass


def test_test_firebird_success(monkeypatch):
    monkeypatch.setattr(diagnostics, "connect_firebird", lambda cfg: FakeConnection())
    ok, msg = diagnostics.test_firebird(_cfg())
    assert ok is True
    assert "2265" in msg


def test_test_firebird_failure(monkeypatch):
    def boom(cfg):
        raise RuntimeError("could not connect")
    monkeypatch.setattr(diagnostics, "connect_firebird", boom)
    ok, msg = diagnostics.test_firebird(_cfg())
    assert ok is False
    assert "could not connect" in msg


def test_test_woocommerce_missing_config():
    cfg = copy.deepcopy(DEFAULT_CONFIG)  # site_url/keys left blank
    ok, msg = diagnostics.test_woocommerce(cfg)
    assert ok is False
    assert "not filled in" in msg


def test_test_woocommerce_success():
    resp = MagicMock()
    resp.status_code = 200
    resp.content = b"[]"
    resp.json.return_value = []

    with patch("requests.request", return_value=resp):
        ok, msg = diagnostics.test_woocommerce(_cfg())
    assert ok is True


def test_test_woocommerce_failure():
    resp = MagicMock()
    resp.status_code = 401
    resp.content = b"{}"
    resp.text = "Unauthorized"

    with patch("requests.request", return_value=resp):
        ok, msg = diagnostics.test_woocommerce(_cfg())
    assert ok is False
    assert "401" in msg


def test_test_connections_combines_both(monkeypatch):
    monkeypatch.setattr(diagnostics, "test_firebird", lambda cfg: (True, "fb ok"))
    monkeypatch.setattr(diagnostics, "test_woocommerce", lambda cfg: (False, "wc bad"))
    result = diagnostics.test_connections(_cfg())
    assert result["firebird"] == {"ok": True, "message": "fb ok"}
    assert result["woocommerce"] == {"ok": False, "message": "wc bad"}


class _RowsCursor:
    def __init__(self, rows):
        self.rows = rows
        self.executed = []

    def execute(self, sql, params=None):
        self.executed.append(sql)

    def fetchall(self):
        return self.rows


class _RowsConnection:
    def __init__(self, rows):
        self.rows = rows
        self.closed = False

    def cursor(self):
        return _RowsCursor(self.rows)

    def close(self):
        self.closed = True


def test_check_piece_annulee_returns_grouped_rows(monkeypatch):
    rows = [("WC-imported", "0", 42), ("other", "N", 900)]
    con = _RowsConnection(rows)
    monkeypatch.setattr(diagnostics, "connect_firebird", lambda cfg: con)
    result = diagnostics.check_piece_annulee(_cfg())
    assert result == rows
    assert con.closed is True


def test_check_duplicate_wc_orders_returns_grouped_rows(monkeypatch):
    rows = [("WC-123", "PC_VE_COM", 3)]
    con = _RowsConnection(rows)
    monkeypatch.setattr(diagnostics, "connect_firebird", lambda cfg: con)
    result = diagnostics.check_duplicate_wc_orders(_cfg())
    assert result == rows
    assert con.closed is True


def test_lookup_pieces_by_refdoc_returns_rows(monkeypatch):
    rows = [("15", "PC_VE_B", "2026-07-06", 6900.0, 0),
            ("45", "PC_VE_B", "2026-07-06", -6900.0, 1)]
    con = _RowsConnection(rows)
    monkeypatch.setattr(diagnostics, "connect_firebird", lambda cfg: con)
    result = diagnostics.lookup_pieces_by_refdoc(_cfg(), "WC-18226")
    assert result == rows
    assert con.closed is True


def test_list_table_columns_decodes_field_types(monkeypatch):
    # (name, RDB$FIELD_TYPE, length, subtype, null_flag)
    raw = [("NOPIECE", 37, 15, 0, 1), ("MONTANTTTC", 27, 8, 0, None)]
    monkeypatch.setattr(diagnostics, "connect_firebird", lambda cfg: _RowsConnection([]))
    monkeypatch.setattr(diagnostics, "list_columns", lambda cur, table: raw)
    result = diagnostics.list_table_columns(_cfg(), "PIECE")
    assert result == [
        ("NOPIECE", "VARCHAR", 15, 0, True),
        ("MONTANTTTC", "DOUBLE PRECISION", 8, 0, False),
    ]


class _BlobLike:
    def __init__(self, data):
        self._data = data

    def read(self):
        return self._data


def test_list_table_triggers_decodes_type_and_reads_blob_source(monkeypatch):
    rows = [
        ("TR_PIECE_BALANCE", 4, 0, _BlobLike(b"UPDATE TIERS SET SOLDE = ...")),  # AFTER UPDATE
        ("TR_PIECE_INACTIVE", 999, 1, "some source"),
    ]
    con = _RowsConnection(rows)
    monkeypatch.setattr(diagnostics, "connect_firebird", lambda cfg: con)
    result = diagnostics.list_table_triggers(_cfg(), "PIECE")
    assert result == [
        ("TR_PIECE_BALANCE", "AFTER UPDATE", False, "UPDATE TIERS SET SOLDE = ..."),
        ("TR_PIECE_INACTIVE", "type=999", True, "some source"),
    ]
