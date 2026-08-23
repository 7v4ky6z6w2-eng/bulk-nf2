import copy

import pytest

from app.config import DEFAULT_CONFIG
from app.sync import yalidine_reconcile as yr


def _cfg(**overrides):
    cfg = copy.deepcopy(DEFAULT_CONFIG)
    cfg["woocommerce"]["site_url"] = "https://example.com"
    cfg["woocommerce"]["consumer_key"] = "ck"
    cfg["woocommerce"]["consumer_secret"] = "cs"
    cfg["yalidine"]["api_id"] = "yid"
    cfg["yalidine"]["api_token"] = "ytoken"
    cfg.update(overrides)
    return cfg


def _order(order_id, tracking=None):
    meta = [{"key": "_yalidine_tracking", "value": tracking}] if tracking else []
    return {"id": order_id, "meta_data": meta}


class FakeCursor:
    def __init__(self, rows):
        self.rows = rows

    def execute(self, sql, params=None):
        pass

    def fetchall(self):
        return self.rows


class FakeConnection:
    def __init__(self, rows):
        self._cur = FakeCursor(rows)

    def cursor(self):
        return self._cur

    def close(self):
        pass


def _setup(monkeypatch, orders, parcels_by_tracking, piece_rows):
    """piece_rows: [(refdoc, montant, annulee), ...]"""
    monkeypatch.setattr(yr, "connect_firebird", lambda cfg: FakeConnection(piece_rows))

    class FakeWCOrdersClient:
        def __init__(self, *a, **kw):
            pass

        def fetch_all_orders(self, after=None):
            return orders

    class FakeYalidineClient:
        def __init__(self, *a, **kw):
            pass

        def fetch_parcels_by_tracking(self, trackings, log_fn=None):
            return {t: parcels_by_tracking[t] for t in trackings if t in parcels_by_tracking}

    monkeypatch.setattr(yr, "WCOrdersClient", FakeWCOrdersClient)
    monkeypatch.setattr(yr, "YalidineClient", FakeYalidineClient)


def test_matched_order_counts_toward_total_not_itemized(monkeypatch):
    _setup(
        monkeypatch,
        orders=[_order(1, "yal-1")],
        parcels_by_tracking={"yal-1": {"status": "Livré", "price": 1000.0}},
        piece_rows=[("WC-1", 1000.0, 1)],
    )
    report = yr.run_reconciliation(_cfg(), "CLI-LIV", "PC_VE_B")
    assert report["matched_count"] == 1
    assert report["matched_total"] == 1000.0
    assert report["amount_mismatch"] == []
    assert report["net_gap"] == 0.0


def test_amount_mismatch_is_itemized_with_diff(monkeypatch):
    _setup(
        monkeypatch,
        orders=[_order(1, "yal-1")],
        parcels_by_tracking={"yal-1": {"status": "Livré", "price": 1200.0}},
        piece_rows=[("WC-1", 1000.0, 1)],
    )
    report = yr.run_reconciliation(_cfg(), "CLI-LIV", "PC_VE_B")
    assert report["amount_mismatch"] == [
        {"order_id": 1, "tracking": "yal-1", "yalidine_price": 1200.0,
         "netfact_montant": 1000.0, "diff": 200.0}
    ]
    assert report["net_gap"] == 200.0


def test_missing_in_netfact_when_delivered_but_no_piece(monkeypatch):
    _setup(
        monkeypatch,
        orders=[_order(1, "yal-1")],
        parcels_by_tracking={"yal-1": {"status": "Livré", "price": 500.0}},
        piece_rows=[],
    )
    report = yr.run_reconciliation(_cfg(), "CLI-LIV", "PC_VE_B")
    assert report["missing_in_netfact"] == [
        {"order_id": 1, "tracking": "yal-1", "yalidine_price": 500.0}
    ]
    assert report["net_gap"] == 500.0


def test_missing_in_yalidine_when_piece_exists_but_never_dispatched(monkeypatch):
    _setup(
        monkeypatch,
        orders=[_order(1)],  # no tracking -- never dispatched
        parcels_by_tracking={},
        piece_rows=[("WC-1", 700.0, 1)],
    )
    report = yr.run_reconciliation(_cfg(), "CLI-LIV", "PC_VE_B")
    assert report["missing_in_yalidine"] == [{"refdoc": "WC-1", "netfact_montant": 700.0}]
    assert report["net_gap"] == -700.0


def test_returned_status_with_still_billed_netfact_document(monkeypatch):
    _setup(
        monkeypatch,
        orders=[_order(1, "yal-1")],
        parcels_by_tracking={"yal-1": {"status": "Retourné au vendeur", "price": 900.0}},
        piece_rows=[("WC-1", 900.0, 1)],
    )
    report = yr.run_reconciliation(_cfg(), "CLI-LIV", "PC_VE_B")
    assert report["returned_or_failed_but_billed"] == [
        {"order_id": 1, "tracking": "yal-1", "status": "Retourné au vendeur", "netfact_montant": 900.0}
    ]
    assert report["net_gap"] == -900.0


def test_in_transit_is_counted_but_not_itemized(monkeypatch):
    _setup(
        monkeypatch,
        orders=[_order(1, "yal-1")],
        parcels_by_tracking={"yal-1": {"status": "En cours de livraison", "price": 300.0}},
        piece_rows=[],
    )
    report = yr.run_reconciliation(_cfg(), "CLI-LIV", "PC_VE_B")
    assert report["in_transit_count"] == 1
    assert report["missing_in_netfact"] == []
    assert report["net_gap"] == 0.0


def test_cancelled_piece_excluded_from_netfact_totals(monkeypatch):
    _setup(
        monkeypatch,
        orders=[_order(1, "yal-1")],
        parcels_by_tracking={"yal-1": {"status": "Livré", "price": 500.0}},
        piece_rows=[("WC-1", 500.0, 0)],  # ANNULEE=0 -- cancelled, per this codebase's convention
    )
    report = yr.run_reconciliation(_cfg(), "CLI-LIV", "PC_VE_B")
    # cancelled doc doesn't count as a real netfact document -- treated as missing
    assert report["missing_in_netfact"] == [
        {"order_id": 1, "tracking": "yal-1", "yalidine_price": 500.0}
    ]


def test_duplicate_piece_rows_for_same_refdoc_are_summed(monkeypatch):
    _setup(
        monkeypatch,
        orders=[_order(1, "yal-1")],
        parcels_by_tracking={"yal-1": {"status": "Livré", "price": 1500.0}},
        piece_rows=[("WC-1", 1000.0, 1), ("WC-1", 500.0, 1)],
    )
    report = yr.run_reconciliation(_cfg(), "CLI-LIV", "PC_VE_B")
    assert report["matched_count"] == 1
    assert report["matched_total"] == 1500.0


def test_missing_yalidine_api_credentials_raises(monkeypatch):
    with pytest.raises(yr.YalidineError):
        yr.run_reconciliation(_cfg(yalidine={"api_id": "", "api_token": ""}), "CLI-LIV", "PC_VE_B")
