import copy

from app.config import DEFAULT_CONFIG
from app.sync.order_importer import OrderImporter


class FakeCursor:
    """Routes execute() to a callback keyed by SQL content, and records
    every statement executed so tests can assert on side effects (e.g.
    that no INSERT happened during a dry run)."""

    def __init__(self, responder):
        self.responder = responder
        self.executed = []
        self._last = None

    def execute(self, sql, params=None):
        self.executed.append((sql, params))
        self._last = self.responder(sql, params)

    def fetchone(self):
        return self._last

    def fetchall(self):
        return self._last if isinstance(self._last, list) else []


class FakeConnection:
    def __init__(self, cur):
        self._cur = cur
        self.committed = False
        self.rolled_back = False

    def cursor(self):
        return self._cur

    def commit(self):
        self.committed = True

    def rollback(self):
        self.rolled_back = True

    def close(self):
        pass


def _cfg(**overrides):
    cfg = copy.deepcopy(DEFAULT_CONFIG)
    cfg["order_import"].update({
        "client_code": "CF01A00002",
        "code_depot": "DP00001",
        "username": "WCSYNC",
        "on_missing_sku": "skip_line",
        "status_mapping": {"processing": "PC_VE_COM", "completed": "PC_VE_B"},
        "transformation": {"PC_VE_B": "PC_VE_COM"},
    })
    cfg["order_import"].update(overrides)
    return cfg


def _wc_order(order_id=555, status="processing", sku="REF1", qty=2, price=24.0):
    return {
        "id": order_id,
        "status": status,
        "date_created": "2026-01-15T14:23:45",
        "billing": {"first_name": "Jean", "last_name": "Dupont", "phone": "0555000000"},
        "shipping": {},
        "line_items": [{"sku": sku, "quantity": qty, "price": price, "product_id": 10}],
    }


def _responder(article_by_sku=None, already_imported=None, source_piece=None,
                max_nopiece=100, max_noitem=500, gen_piece=0, gen_item=0,
                existing_docs=None):
    article_by_sku = article_by_sku or {"REF1": ("REF1", 20.0, 24.0, 19.0)}
    already_imported = already_imported or {}
    source_piece = source_piece or {}
    existing_docs = existing_docs or {}

    def responder(sql, params):
        s = sql.upper()
        if "RAISON_SOCIALE FROM TIERS" in s:
            return ("Client Livraison Web",)
        if "GEN_ID(NEXTPIECE" in s:
            return (gen_piece,)
        if "GEN_ID(NEXTITEM" in s:
            return (gen_item,)
        if "MAX(CAST(NOPIECE" in s:
            return (max_nopiece,)
        if "MAX(CAST(NOITEM" in s:
            return (max_noitem,)
        if "ANNULEE FROM PIECE WHERE REFDOC" in s:
            (refdoc,) = params
            return list(existing_docs.get(refdoc, []))
        if "NOPIECE FROM PIECE WHERE REFDOC" in s:
            refdoc, doc_type = params
            key = (refdoc, doc_type)
            if key in already_imported:
                return (already_imported[key],)
            if key in source_piece:
                return (source_piece[key],)
            return None
        if "REF_ART, PRIXVENTEHT, PRIXVENTETTC, TAUX_TVA FROM ARTICLE" in s:
            (sku,) = params
            return article_by_sku.get(sku)
        return None

    return responder


def _make_importer(cfg, responder):
    cur = FakeCursor(responder)
    con = FakeConnection(cur)
    return OrderImporter(con, cfg), con, cur


def test_next_base_uses_higher_of_max_existing_and_generator():
    cfg = _cfg()
    importer, con, cur = _make_importer(cfg, _responder(max_nopiece=100, gen_piece=250))
    assert importer._next_base("NEXTPIECE", "PIECE", "NOPIECE") == 250  # generator ahead of MAX

    importer2, _, _ = _make_importer(cfg, _responder(max_nopiece=900, gen_piece=250))
    assert importer2._next_base("NEXTPIECE", "PIECE", "NOPIECE") == 900  # MAX ahead of generator


def test_status_not_mapped_is_skipped():
    cfg = _cfg()
    importer, con, cur = _make_importer(cfg, _responder())
    status, msg, reason = importer.import_order(_wc_order(status="on-hold"))
    assert status == "skipped"
    assert "not mapped" in msg
    assert reason == "status_not_mapped"


def test_already_imported_order_is_skipped_not_duplicated():
    cfg = _cfg()
    responder = _responder(already_imported={("WC-555", "PC_VE_COM"): "42"})
    importer, con, cur = _make_importer(cfg, responder)
    status, msg, reason = importer.import_order(_wc_order(order_id=555, status="processing"))
    assert status == "skipped"
    assert "NOPIECE=42" in msg
    assert reason == "already_imported"
    assert not any("INSERT INTO PIECE" in sql for sql, _ in cur.executed)


def test_dry_run_does_not_write_or_commit(monkeypatch):
    cfg = _cfg()
    importer, con, cur = _make_importer(cfg, _responder())
    status, msg, reason = importer.import_order(_wc_order(), dry_run=True)
    assert status == "skipped"
    assert msg.startswith("[DRY]")
    assert not any("INSERT" in sql.upper() for sql, _ in cur.executed)
    assert con.committed is False


def test_missing_sku_skip_order_policy_errors_whole_order():
    cfg = _cfg(on_missing_sku="skip_order")
    importer, con, cur = _make_importer(cfg, _responder(article_by_sku={}))
    status, msg, reason = importer.import_order(_wc_order(sku="UNKNOWN_SKU"))
    assert status == "error"
    assert "UNKNOWN_SKU" in msg


def test_missing_sku_skip_line_policy_skips_just_that_line():
    cfg = _cfg(on_missing_sku="skip_line")
    importer, con, cur = _make_importer(cfg, _responder(article_by_sku={}))
    status, msg, reason = importer.import_order(_wc_order(sku="UNKNOWN_SKU"))
    # No valid lines remain -> the whole order is skipped, but NOT an error.
    assert status == "skipped"
    assert msg == "no valid lines"


def test_creates_piece_and_items_with_correct_generator_based_ids():
    cfg = _cfg()
    responder = _responder(max_nopiece=100, gen_piece=250, max_noitem=500, gen_item=10)
    importer, con, cur = _make_importer(cfg, responder)

    status, msg, reason = importer.import_order(_wc_order(order_id=999, sku="REF1", qty=3))

    assert status == "created"
    assert "NOPIECE=251" in msg  # max(100, 250) + 1
    assert con.committed is True

    insert_piece = [p for sql, p in cur.executed if "INSERT INTO PIECE" in sql]
    assert len(insert_piece) == 1
    assert insert_piece[0][0] == "251"  # NOPIECE
    assert insert_piece[0][5] == "WC-999"  # REFDOC

    insert_items = [p for sql, p in cur.executed if "INSERT INTO ITEM" in sql]
    assert len(insert_items) == 1
    assert insert_items[0][0] == "501"  # NOITEM = max(500, 10) + 1
    assert insert_items[0][1] == "251"  # NOPIECE FK
    assert insert_items[0][3] == 3.0    # QTE


def test_transformation_links_to_source_piece():
    cfg = _cfg()
    responder = _responder(
        source_piece={("WC-321", "PC_VE_COM"): "77"},
    )
    importer, con, cur = _make_importer(cfg, responder)
    status, msg, reason = importer.import_order(_wc_order(order_id=321, status="completed"))
    assert status == "created"
    assert "linked from PC_VE_COM#77" in msg
    update_calls = [p for sql, p in cur.executed if sql.startswith("UPDATE PIECE SET NOPIECE_T")]
    assert len(update_calls) == 1
    assert update_calls[0][1] == "77"  # source NOPIECE being updated


def test_run_order_import_tallies_skip_reasons(monkeypatch):
    from app.sync import order_importer

    cfg = _cfg()
    cfg["woocommerce"]["site_url"] = "https://example.com"
    cfg["woocommerce"]["consumer_key"] = "ck"
    cfg["woocommerce"]["consumer_secret"] = "cs"

    responder = _responder(already_imported={("WC-1", "PC_VE_COM"): "10"})
    cur = FakeCursor(responder)
    con = FakeConnection(cur)

    orders = [
        _wc_order(order_id=1, status="processing"),  # already imported -> skipped
        _wc_order(order_id=2, status="on-hold"),      # status not mapped -> skipped
        _wc_order(order_id=3, status="processing"),  # new -> created
    ]

    class FakeWCOrdersClient:
        def __init__(self, *args, **kwargs):
            pass

        def fetch_orders(self, statuses, after=None):
            return orders

        def get_variation_sku(self, *args, **kwargs):
            return ""

        def get_product_sku(self, *args, **kwargs):
            return ""

    monkeypatch.setattr(order_importer, "connect_firebird", lambda cfg: con)
    monkeypatch.setattr(order_importer, "WCOrdersClient", FakeWCOrdersClient)

    report = order_importer.run_order_import(cfg)

    assert report["created"] == [3]
    assert set(report["skipped"]) == {1, 2}
    assert report["skip_reasons"] == {"already_imported": 1, "status_not_mapped": 1}
    assert report["errors"] == []


def test_cancel_order_annuls_active_documents():
    cfg = _cfg()
    responder = _responder(existing_docs={"WC-777": [("501", "PC_VE_COM", 0)]})
    importer, con, cur = _make_importer(cfg, responder)

    status, msg, reason = importer.cancel_order(_wc_order(order_id=777, status="cancelled"))

    assert status == "cancelled"
    assert "PC_VE_COM#501" in msg
    assert reason is None
    assert con.committed is True
    piece_updates = [p for sql, p in cur.executed if sql.startswith("UPDATE PIECE SET ANNULEE")]
    item_updates = [p for sql, p in cur.executed if sql.startswith("UPDATE ITEM SET ANNULEE")]
    assert piece_updates == [("501",)]
    assert item_updates == [("501",)]


def test_cancel_order_annuls_all_active_documents_for_the_order():
    # An order can accumulate more than one PIECE over time via
    # transformation linking (e.g. commande then bon de livraison) --
    # cancelling must annul all of them, not just one.
    cfg = _cfg()
    responder = _responder(existing_docs={
        "WC-42": [("10", "PC_VE_COM", 0), ("11", "PC_VE_B", 0)],
    })
    importer, con, cur = _make_importer(cfg, responder)

    status, msg, reason = importer.cancel_order(_wc_order(order_id=42, status="cancelled"))

    assert status == "cancelled"
    piece_updates = {p[0] for sql, p in cur.executed if sql.startswith("UPDATE PIECE SET ANNULEE")}
    assert piece_updates == {"10", "11"}


def test_cancel_order_with_no_existing_document_is_skipped():
    cfg = _cfg()
    importer, con, cur = _make_importer(cfg, _responder())
    status, msg, reason = importer.cancel_order(_wc_order(order_id=999, status="cancelled"))
    assert status == "skipped"
    assert reason == "no_existing_document"
    assert con.committed is False


def test_cancel_order_already_cancelled_is_skipped_not_reannuled():
    cfg = _cfg()
    responder = _responder(existing_docs={"WC-5": [("20", "PC_VE_COM", 1)]})
    importer, con, cur = _make_importer(cfg, responder)
    status, msg, reason = importer.cancel_order(_wc_order(order_id=5, status="cancelled"))
    assert status == "skipped"
    assert reason == "already_cancelled"
    assert not any(sql.startswith("UPDATE") for sql, _ in cur.executed)


def test_cancel_order_dry_run_does_not_write():
    cfg = _cfg()
    responder = _responder(existing_docs={"WC-8": [("30", "PC_VE_COM", 0)]})
    importer, con, cur = _make_importer(cfg, responder)
    status, msg, reason = importer.cancel_order(_wc_order(order_id=8, status="cancelled"), dry_run=True)
    assert status == "skipped"
    assert msg.startswith("[DRY]")
    assert not any(sql.startswith("UPDATE") for sql, _ in cur.executed)
    assert con.committed is False


def test_run_order_import_dispatches_cancel_statuses_to_cancel_order(monkeypatch):
    from app.sync import order_importer

    cfg = _cfg(cancel_statuses=["cancelled"])
    cfg["woocommerce"]["site_url"] = "https://example.com"
    cfg["woocommerce"]["consumer_key"] = "ck"
    cfg["woocommerce"]["consumer_secret"] = "cs"

    responder = _responder(existing_docs={"WC-9": [("60", "PC_VE_COM", 0)]})
    cur = FakeCursor(responder)
    con = FakeConnection(cur)

    orders = [
        _wc_order(order_id=9, status="cancelled"),   # had a document -> cancelled
        _wc_order(order_id=10, status="processing"),  # new -> created
    ]

    class FakeWCOrdersClient:
        def __init__(self, *args, **kwargs):
            pass

        def fetch_orders(self, statuses, after=None):
            return orders

        def get_variation_sku(self, *args, **kwargs):
            return ""

        def get_product_sku(self, *args, **kwargs):
            return ""

    monkeypatch.setattr(order_importer, "connect_firebird", lambda cfg: con)
    monkeypatch.setattr(order_importer, "WCOrdersClient", FakeWCOrdersClient)

    report = order_importer.run_order_import(cfg)

    assert report["cancelled"] == [9]
    assert report["created"] == [10]
    assert report["errors"] == []


def test_safe_text_replaces_characters_the_connection_charset_cant_encode():
    cfg = _cfg()
    cfg["firebird"]["charset"] = "WIN1256"
    importer, con, cur = _make_importer(cfg, _responder())
    # ڨ (Kurdish KAF) isn't representable in Windows-1256 -- must not
    # raise, so one customer's name can't take down the whole order.
    result = importer._safe_text("مڨان")
    assert result is not None
    result.encode("cp1256")  # no UnicodeEncodeError


def test_import_order_sanitizes_unencodable_customer_name():
    cfg = _cfg()
    cfg["firebird"]["charset"] = "WIN1256"
    importer, con, cur = _make_importer(cfg, _responder())
    order = _wc_order(order_id=321)
    order["billing"]["first_name"] = "ڨاک"  # contains Kurdish KAF
    status, msg, reason = importer.import_order(order)
    assert status == "created"
