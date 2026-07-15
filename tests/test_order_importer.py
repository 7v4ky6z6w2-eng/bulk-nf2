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
                existing_docs=None, type_piece_coeffs=None):
    article_by_sku = article_by_sku or {"REF1": ("REF1", 20.0, 24.0, 19.0)}
    already_imported = already_imported or {}
    source_piece = source_piece or {}
    existing_docs = existing_docs or {}
    type_piece_coeffs = type_piece_coeffs or {}

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
        if "COEFF_PIECE, COEFF_PIECE_TR, COEFF_ITEM, COEFF_ITEM_TR FROM LOCAL_TYPE_PIECE" in s:
            (code_type_piece,) = params
            return type_piece_coeffs.get(code_type_piece)
        if "NOPIECE, CODE_TYPE_PIECE, ANNULEE FROM PIECE WHERE REFDOC" in s:
            (refdoc,) = params
            return list(existing_docs.get(refdoc, []))
        if "NOPIECE, ANNULEE FROM PIECE WHERE REFDOC" in s:
            refdoc, doc_type = params
            key = (refdoc, doc_type)
            if key in already_imported:
                val = already_imported[key]
                return val if isinstance(val, list) else [(val, 1)]  # 1 = active
            if key in source_piece:
                val = source_piece[key]
                return val if isinstance(val, list) else [(val, 1)]  # 1 = active
            return []
        if "REF_ART, PRIXVENTEHT, PRIXVENTETTC, TAUX_TVA FROM ARTICLE" in s:
            (sku,) = params
            return article_by_sku.get(sku)
        return None

    return responder


def _make_importer(cfg, responder):
    cur = FakeCursor(responder)
    con = FakeConnection(cur)
    return OrderImporter(con, cfg), con, cur


def test_is_annulled_matches_confirmed_netfact2_convention():
    # Confirmed directly against the live NetFact2 install: ANNULEE=1
    # means NOT cancelled (the normal/valid state), ANNULEE=0 means
    # cancelled -- the opposite of what the column's name suggests.
    from app.sync.order_importer import _is_annulled

    for value in (1, "1", True):
        assert _is_annulled(value) is False
    for value in (0, "0", None, False):
        assert _is_annulled(value) is True


def test_already_imported_recognizes_prior_active_doc():
    cfg = _cfg()

    def responder(sql, params):
        s = sql.upper()
        if "RAISON_SOCIALE FROM TIERS" in s:
            return ("Client",)
        if "NOPIECE, ANNULEE FROM PIECE WHERE REFDOC" in s:
            return [("77", 1)]
        return None

    importer, con, cur = _make_importer(cfg, responder)
    assert importer.already_imported(555, "PC_VE_COM") == "77"


def test_already_imported_finds_active_row_even_when_cancelled_duplicate_listed_first():
    # Regression test: an order can end up with two PIECE rows sharing the
    # same REFDOC -- e.g. a leftover duplicate from the historical
    # duplicate-reimport bug that the user cancelled by hand in NetFact2
    # instead of deleting. fetchone() with no ORDER BY could return that
    # cancelled row first and wrongly conclude nothing was ever imported,
    # creating yet another duplicate on the next run.
    cfg = _cfg()
    responder = _responder(already_imported={
        ("WC-555", "PC_VE_COM"): [("41", 0), ("42", 1)],  # cancelled row first
    })
    importer, con, cur = _make_importer(cfg, responder)
    assert importer.already_imported(555, "PC_VE_COM") == "42"


def test_already_imported_returns_none_when_every_matching_row_is_cancelled():
    cfg = _cfg()
    responder = _responder(already_imported={
        ("WC-555", "PC_VE_COM"): [("41", 0), ("42", 0)],
    })
    importer, con, cur = _make_importer(cfg, responder)
    assert importer.already_imported(555, "PC_VE_COM") is None


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
    responder = _responder(max_nopiece=100, gen_piece=250, max_noitem=500, gen_item=10,
                            type_piece_coeffs={"PC_VE_COM": (1, 0, 1, 0)})
    importer, con, cur = _make_importer(cfg, responder)

    status, msg, reason = importer.import_order(_wc_order(order_id=999, sku="REF1", qty=3))

    assert status == "created"
    assert "NOPIECE=251" in msg  # max(100, 250) + 1
    assert con.committed is True

    insert_piece = [p for sql, p in cur.executed if "INSERT INTO PIECE" in sql]
    assert len(insert_piece) == 1
    assert insert_piece[0][0] == "251"  # NOPIECE
    assert insert_piece[0][5] == "WC-999"  # REFDOC
    assert insert_piece[0][9] == 72.0  # MONTANT == MONTANTTTC (3 * 24.0)
    assert insert_piece[0][10] == 1    # COEFF (from LOCAL_TYPE_PIECE.COEFF_PIECE)
    assert insert_piece[0][11] == 0    # COEFF_TR (from LOCAL_TYPE_PIECE.COEFF_PIECE_TR)

    insert_items = [p for sql, p in cur.executed if "INSERT INTO ITEM" in sql]
    assert len(insert_items) == 1
    assert insert_items[0][0] == "501"  # NOITEM = max(500, 10) + 1
    assert insert_items[0][1] == "251"  # NOPIECE FK
    assert insert_items[0][3] == 3.0    # QTE
    assert insert_items[0][6] == 1      # COEFF (from LOCAL_TYPE_PIECE.COEFF_ITEM)
    assert insert_items[0][7] == 0      # COEFF_TR (from LOCAL_TYPE_PIECE.COEFF_ITEM_TR)


def test_type_piece_coefficients_looked_up_from_local_type_piece():
    cfg = _cfg()
    responder = _responder(type_piece_coeffs={"PC_VE_COM": (1, -1, 2, -2)})
    importer, con, cur = _make_importer(cfg, responder)
    assert importer._type_piece_coefficients("PC_VE_COM") == (1, -1, 2, -2)


def test_type_piece_coefficients_defaults_to_zero_when_type_not_found():
    cfg = _cfg()
    importer, con, cur = _make_importer(cfg, _responder())  # no type_piece_coeffs configured
    assert importer._type_piece_coefficients("UNKNOWN_TYPE") == (0, 0, 0, 0)


def test_type_piece_coefficients_cached_after_first_lookup():
    cfg = _cfg()
    responder = _responder(type_piece_coeffs={"PC_VE_COM": (1, 0, 1, 0)})
    importer, con, cur = _make_importer(cfg, responder)

    importer._type_piece_coefficients("PC_VE_COM")
    importer._type_piece_coefficients("PC_VE_COM")

    lookups = [sql for sql, _ in cur.executed if "COEFF_PIECE, COEFF_PIECE_TR" in sql]
    assert len(lookups) == 1


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
    responder = _responder(existing_docs={"WC-777": [("501", "PC_VE_COM", 1)]})
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
        "WC-42": [("10", "PC_VE_COM", 1), ("11", "PC_VE_B", 1)],
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
    responder = _responder(existing_docs={"WC-5": [("20", "PC_VE_COM", 0)]})
    importer, con, cur = _make_importer(cfg, responder)
    status, msg, reason = importer.cancel_order(_wc_order(order_id=5, status="cancelled"))
    assert status == "skipped"
    assert reason == "already_cancelled"
    assert not any(sql.startswith("UPDATE") for sql, _ in cur.executed)


def test_cancel_order_dry_run_does_not_write():
    cfg = _cfg()
    responder = _responder(existing_docs={"WC-8": [("30", "PC_VE_COM", 1)]})
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

    responder = _responder(existing_docs={"WC-9": [("60", "PC_VE_COM", 1)]})
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


def test_normalize_wc_after():
    from app.sync.order_importer import _normalize_wc_after

    assert _normalize_wc_after("") is None
    assert _normalize_wc_after(None) is None
    assert _normalize_wc_after("  ") is None
    assert _normalize_wc_after("2026-01-01") == "2026-01-01T00:00:00"
    assert _normalize_wc_after("2026-01-01T08:30:00") == "2026-01-01T08:30:00"


def test_run_order_import_passes_start_date_as_after_filter(monkeypatch):
    from app.sync import order_importer

    cfg = _cfg(start_date="2026-01-01")
    cfg["woocommerce"]["site_url"] = "https://example.com"
    cfg["woocommerce"]["consumer_key"] = "ck"
    cfg["woocommerce"]["consumer_secret"] = "cs"

    cur = FakeCursor(_responder())
    con = FakeConnection(cur)
    captured = {}

    class FakeWCOrdersClient:
        def __init__(self, *args, **kwargs):
            pass

        def fetch_orders(self, statuses, after=None):
            captured["after"] = after
            return []

        def get_variation_sku(self, *args, **kwargs):
            return ""

        def get_product_sku(self, *args, **kwargs):
            return ""

    monkeypatch.setattr(order_importer, "connect_firebird", lambda cfg: con)
    monkeypatch.setattr(order_importer, "WCOrdersClient", FakeWCOrdersClient)

    order_importer.run_order_import(cfg)

    assert captured["after"] == "2026-01-01T00:00:00"


def test_run_order_import_with_no_start_date_fetches_full_history(monkeypatch):
    from app.sync import order_importer

    cfg = _cfg()  # start_date left at DEFAULT_CONFIG's "" (via _cfg's deepcopy)
    cfg["woocommerce"]["site_url"] = "https://example.com"
    cfg["woocommerce"]["consumer_key"] = "ck"
    cfg["woocommerce"]["consumer_secret"] = "cs"

    cur = FakeCursor(_responder())
    con = FakeConnection(cur)
    captured = {}

    class FakeWCOrdersClient:
        def __init__(self, *args, **kwargs):
            pass

        def fetch_orders(self, statuses, after=None):
            captured["after"] = after
            return []

        def get_variation_sku(self, *args, **kwargs):
            return ""

        def get_product_sku(self, *args, **kwargs):
            return ""

    monkeypatch.setattr(order_importer, "connect_firebird", lambda cfg: con)
    monkeypatch.setattr(order_importer, "WCOrdersClient", FakeWCOrdersClient)

    order_importer.run_order_import(cfg)

    assert captured["after"] is None


def test_fix_duplicate_orders_prefers_active_and_deletes_the_rest(monkeypatch):
    from app.sync import order_importer

    def responder(sql, params):
        s = sql.upper()
        if "REFDOC, CODE_TYPE_PIECE, NOPIECE, ANNULEE FROM PIECE" in s:
            return [
                ("WC-1", "PC_VE_COM", "10", 1),
                ("WC-1", "PC_VE_COM", "20", 1),
                # 1 active + 1 already-cancelled-by-hand: still 2 documents
                # for the same order, so still a live duplicate to clean
                # up -- the actual scenario the user hit ("0 duplicates
                # found" when this was still gated on "> 1 active row").
                ("WC-2", "PC_VE_COM", "30", 1),
                ("WC-2", "PC_VE_COM", "40", 0),
                ("WC-3", "PC_VE_B", "50", 1),  # sole document -> untouched
            ]
        return None

    cur = FakeCursor(responder)
    con = FakeConnection(cur)
    monkeypatch.setattr(order_importer, "connect_firebird", lambda cfg: con)

    report = order_importer.fix_duplicate_orders(_cfg(), dry_run=False)

    assert report["total_deleted"] == 2
    assert report["fixed"] == [
        {"refdoc": "WC-1", "code_type_piece": "PC_VE_COM", "kept": "10", "deleted": ["20"]},
        {"refdoc": "WC-2", "code_type_piece": "PC_VE_COM", "kept": "30", "deleted": ["40"]},
    ]
    piece_deletes = {p[0] for sql, p in cur.executed if sql.startswith("DELETE FROM PIECE")}
    item_deletes = {p[0] for sql, p in cur.executed if sql.startswith("DELETE FROM ITEM")}
    assert piece_deletes == {"20", "40"}
    assert item_deletes == {"20", "40"}
    assert con.committed is True


def test_fix_duplicate_orders_keeps_earliest_when_none_are_active(monkeypatch):
    # Edge case: both copies still sitting cancelled (neither manually
    # fixed yet). Nothing to prefer, so keep the earliest by NOPIECE.
    from app.sync import order_importer

    def responder(sql, params):
        s = sql.upper()
        if "REFDOC, CODE_TYPE_PIECE, NOPIECE, ANNULEE FROM PIECE" in s:
            return [("WC-7", "PC_VE_COM", "70", 0), ("WC-7", "PC_VE_COM", "71", 0)]
        return None

    cur = FakeCursor(responder)
    con = FakeConnection(cur)
    monkeypatch.setattr(order_importer, "connect_firebird", lambda cfg: con)

    report = order_importer.fix_duplicate_orders(_cfg(), dry_run=False)

    assert report["fixed"] == [
        {"refdoc": "WC-7", "code_type_piece": "PC_VE_COM", "kept": "70", "deleted": ["71"]},
    ]


def test_fix_duplicate_orders_dry_run_does_not_write(monkeypatch):
    from app.sync import order_importer

    def responder(sql, params):
        s = sql.upper()
        if "REFDOC, CODE_TYPE_PIECE, NOPIECE, ANNULEE FROM PIECE" in s:
            return [("WC-1", "PC_VE_COM", "10", 1), ("WC-1", "PC_VE_COM", "20", 1)]
        return None

    cur = FakeCursor(responder)
    con = FakeConnection(cur)
    monkeypatch.setattr(order_importer, "connect_firebird", lambda cfg: con)

    report = order_importer.fix_duplicate_orders(_cfg(), dry_run=True)

    assert report["total_deleted"] == 1
    assert report["fixed"][0]["deleted"] == ["20"]
    assert not any(sql.startswith("DELETE") for sql, _ in cur.executed)
    assert con.committed is False


def test_fix_duplicate_orders_no_duplicates_is_a_noop(monkeypatch):
    from app.sync import order_importer

    def responder(sql, params):
        s = sql.upper()
        if "REFDOC, CODE_TYPE_PIECE, NOPIECE, ANNULEE FROM PIECE" in s:
            return [("WC-1", "PC_VE_COM", "10", 1), ("WC-2", "PC_VE_COM", "30", 1)]
        return None

    cur = FakeCursor(responder)
    con = FakeConnection(cur)
    monkeypatch.setattr(order_importer, "connect_firebird", lambda cfg: con)

    report = order_importer.fix_duplicate_orders(_cfg(), dry_run=False)

    assert report["fixed"] == []
    assert report["total_deleted"] == 0
