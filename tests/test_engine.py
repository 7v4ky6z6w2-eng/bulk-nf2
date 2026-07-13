import copy
import json
import os
import tempfile

from app.config import DEFAULT_CONFIG
from app.sync import engine


def _cfg(state_db_path):
    cfg = copy.deepcopy(DEFAULT_CONFIG)
    cfg["state_db_path"] = state_db_path
    cfg["woocommerce"]["site_url"] = "https://example.com"
    cfg["woocommerce"]["consumer_key"] = "ck"
    cfg["woocommerce"]["consumer_secret"] = "cs"
    return cfg


def _article(**overrides):
    base = {
        "ref_art": "REF1",
        "designation": "Stylo bille bleu",
        "codefamille": "FAM1",
        "famille_intitule": "Stylos",
        "prix_achat_ht": 10.0,
        "prix_achat_ttc": 12.0,
        "prix_vente_ht": 20.0,
        "prix_vente_ttc": 24.0,
        "prix_ht_promo": None,
        "prix_ttc_promo": None,
        "active_promo": False,
        "date_deb_promo": None,
        "date_fin_promo": None,
        "taux_tva": 19,
        "ctrl_stock": True,
        "photo": None,
        "barcodes": [],
        "stock_qty": 5,
    }
    base.update(overrides)
    return base


class DummyConnection:
    def close(self):
        pass


def test_build_core_fields_no_active_promo():
    article = _article(prix_ttc_promo=15.0, active_promo=False)
    cfg = _cfg("unused.sqlite3")
    core = engine.build_core_fields(article, cfg)
    assert core["regular_price"] == "24.00"
    assert "sale_price" not in core


def test_build_core_fields_active_promo_uses_dates():
    article = _article(
        prix_ttc_promo=15.0, active_promo=True,
        date_deb_promo="2026-01-01", date_fin_promo="2026-01-31",
    )
    cfg = _cfg("unused.sqlite3")
    core = engine.build_core_fields(article, cfg)
    assert core["sale_price"] == "15.00"
    assert core["date_on_sale_from"] == "2026-01-01"
    assert core["date_on_sale_to"] == "2026-01-31"


def test_build_core_fields_no_stock_qty_omits_manage_stock():
    # Matches the real DIFA2.FDB install: no STOCK/FICHE_STOCK table exists,
    # so queries.fetch_stock_quantities() always leaves stock_qty as None.
    article = _article(stock_qty=None)
    cfg = _cfg("unused.sqlite3")
    core = engine.build_core_fields(article, cfg)
    assert core["manage_stock"] is False
    assert "stock_quantity" not in core


def test_build_core_fields_with_stock_qty_sets_manage_stock():
    article = _article(stock_qty=15)
    cfg = _cfg("unused.sqlite3")
    core = engine.build_core_fields(article, cfg)
    assert core["manage_stock"] is True
    assert core["stock_quantity"] == 15


def test_build_core_fields_barcodes():
    article = _article(barcodes=["1111111111", "2222222222", "3333333333"])
    cfg = _cfg("unused.sqlite3")
    core = engine.build_core_fields(article, cfg)
    assert core["global_unique_id"] == "1111111111"
    meta = {m["key"]: m["value"] for m in core["meta_data"]}
    assert meta["_barcode"] == "1111111111"
    assert meta["_alt_barcodes"] == "2222222222,3333333333"


def test_build_core_fields_does_not_set_category():
    # Categories are intentionally left untouched -- the store runs its own
    # WordPress auto-categorizer plugin instead.
    article = _article(designation="STYL BIL BLU")
    cfg = _cfg("unused.sqlite3")
    core = engine.build_core_fields(article, cfg)
    assert core["name"] == "Stylo Bille Bleu"
    assert "categories" not in core
    assert not any(k.startswith("_category") for k in core)


def test_content_hash_changes_when_price_changes():
    cfg = _cfg("unused.sqlite3")
    a1 = engine.build_core_fields(_article(prix_vente_ttc=24.0), cfg)
    a2 = engine.build_core_fields(_article(prix_vente_ttc=30.0), cfg)
    assert engine.content_hash(a1, False) != engine.content_hash(a2, False)


def test_run_sync_dry_run_does_not_touch_state(monkeypatch):
    with tempfile.TemporaryDirectory() as tmp:
        state_path = os.path.join(tmp, "state.sqlite3")
        cfg = _cfg(state_path)

        monkeypatch.setattr(engine, "connect_firebird", lambda cfg: DummyConnection())
        monkeypatch.setattr(engine.queries, "fetch_familles", lambda con: {
            "FAM1": {"intitule": "Stylos", "boutiq_visible": True}
        })
        monkeypatch.setattr(engine.queries, "fetch_articles",
                             lambda con, familles, filter_boutique_visible: [_article()])

        report = engine.run_sync(cfg, dry_run=True)

        assert len(report["payloads"]) == 1
        assert report["payloads"][0]["action"] == "create"
        assert not os.path.exists(state_path) or _row_count(state_path) == 0


def _row_count(path):
    import sqlite3
    con = sqlite3.connect(path)
    try:
        return con.execute("SELECT COUNT(*) FROM sync_map").fetchone()[0]
    finally:
        con.close()


class FakeWooCommerceClient:
    """Stand-in for WooCommerceClient that never touches the network."""
    instances = []

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.created = []
        FakeWooCommerceClient.instances.append(self)

    def batch_products(self, create=None, update=None, delete=None, chunk_size=100, progress_fn=None):
        create = create or []
        update = update or []
        next_id = 1000
        out_create = []
        for item in create:
            out_create.append({**item, "id": next_id})
            next_id += 1
        out_update = [{**item, "id": item["id"]} for item in update]
        return {"create": out_create, "update": out_update, "delete": []}


def test_run_sync_real_run_then_unchanged_on_rerun(monkeypatch):
    with tempfile.TemporaryDirectory() as tmp:
        state_path = os.path.join(tmp, "state.sqlite3")
        cfg = _cfg(state_path)

        monkeypatch.setattr(engine, "connect_firebird", lambda cfg: DummyConnection())
        monkeypatch.setattr(engine.queries, "fetch_familles", lambda con: {
            "FAM1": {"intitule": "Stylos", "boutiq_visible": True}
        })
        monkeypatch.setattr(engine.queries, "fetch_articles",
                             lambda con, familles, filter_boutique_visible: [_article()])
        monkeypatch.setattr(engine, "WooCommerceClient", FakeWooCommerceClient)

        report1 = engine.run_sync(cfg, dry_run=False)
        assert report1["created"] == ["REF1"]
        assert _row_count(state_path) == 1

        report2 = engine.run_sync(cfg, dry_run=False)
        assert report2["created"] == []
        assert report2["updated"] == []
        assert report2["unchanged"] == 1


def test_run_sync_reports_orphans(monkeypatch):
    with tempfile.TemporaryDirectory() as tmp:
        state_path = os.path.join(tmp, "state.sqlite3")
        cfg = _cfg(state_path)

        monkeypatch.setattr(engine, "connect_firebird", lambda cfg: DummyConnection())
        monkeypatch.setattr(engine.queries, "fetch_familles", lambda con: {
            "FAM1": {"intitule": "Stylos", "boutiq_visible": True}
        })
        monkeypatch.setattr(engine.queries, "fetch_articles",
                             lambda con, familles, filter_boutique_visible: [_article()])
        monkeypatch.setattr(engine, "WooCommerceClient", FakeWooCommerceClient)

        engine.run_sync(cfg, dry_run=False)

        # Now the article "disappears" from Firebird (e.g. REF_ART renamed).
        monkeypatch.setattr(engine.queries, "fetch_articles",
                             lambda con, familles, filter_boutique_visible: [])
        report = engine.run_sync(cfg, dry_run=False)
        assert report["orphans"] == ["REF1"]


def test_render_report_lines_truncates_large_payload_lists():
    report = {
        "payloads": [{"ref_art": f"R{i}", "action": "create", "payload": {}} for i in range(30)],
        "orphans": [], "errors": [],
    }
    lines = engine.render_report_lines(report, payload_limit=5)
    assert lines[0] == "--- Dry-run payloads (30 total) ---"
    assert len(lines) == 1 + 5 + 1  # header + 5 shown + "... and N more"
    assert "25 more" in lines[-1]


def test_render_report_lines_no_limit_shows_everything():
    report = {
        "payloads": [{"ref_art": f"R{i}", "action": "create", "payload": {}} for i in range(3)],
        "orphans": [], "errors": [],
    }
    lines = engine.render_report_lines(report, payload_limit=None)
    assert len(lines) == 1 + 3  # header + all 3, no truncation note


def test_write_dry_run_payloads_writes_file_and_returns_path():
    with tempfile.TemporaryDirectory() as tmp:
        out_path = os.path.join(tmp, "out.json")
        report = {"payloads": [{"ref_art": "R1", "action": "create", "payload": {"sku": "R1"}}]}
        result = engine.write_dry_run_payloads(report, path=out_path)
        assert result == out_path
        with open(out_path, encoding="utf-8") as fh:
            data = json.load(fh)
        assert data == report["payloads"]


def test_write_dry_run_payloads_returns_none_when_nothing_to_write():
    assert engine.write_dry_run_payloads({"payloads": []}) is None
    assert engine.write_dry_run_payloads({}) is None
