from unittest.mock import patch, MagicMock

import pytest

from app.sync.woocommerce_client import WooCommerceClient, WooCommerceError


def _mock_response(json_data, status_code=200):
    resp = MagicMock()
    resp.status_code = status_code
    resp.content = b"x"
    resp.json.return_value = json_data
    resp.text = str(json_data)
    return resp


def test_batch_products_chunks_at_100():
    client = WooCommerceClient("https://example.com", "ck", "cs")
    create_items = [{"sku": f"S{i}"} for i in range(150)]

    calls = []

    def fake_request(method, url, auth=None, timeout=None, **kwargs):
        calls.append(kwargs.get("json"))
        n_create = len(kwargs["json"].get("create", []))
        return _mock_response({"create": [{"sku": f"S{i}"} for i in range(n_create)]})

    with patch("requests.request", side_effect=fake_request):
        result = client.batch_products(create=create_items)

    assert len(calls) == 2
    assert len(calls[0]["create"]) == 100
    assert len(calls[1]["create"]) == 50
    assert len(result["create"]) == 150


def test_batch_products_reports_progress_per_chunk():
    client = WooCommerceClient("https://example.com", "ck", "cs")
    create_items = [{"sku": f"S{i}"} for i in range(250)]
    progress_calls = []

    def fake_request(method, url, auth=None, timeout=None, **kwargs):
        n_create = len(kwargs["json"].get("create", []))
        return _mock_response({"create": [{"sku": f"S{i}"} for i in range(n_create)]})

    with patch("requests.request", side_effect=fake_request):
        client.batch_products(
            create=create_items,
            progress_fn=lambda *args: progress_calls.append(args),
        )

    assert progress_calls == [(1, 3, 100), (2, 3, 100), (3, 3, 50)]


def test_batch_products_survives_one_chunk_failing():
    # A transient failure on one chunk must not lose the other chunks'
    # results, and must not abort the run -- the failed chunk's items
    # come back as per-item errors instead.
    client = WooCommerceClient("https://example.com", "ck", "cs")
    create_items = [{"sku": f"S{i}"} for i in range(250)]  # 3 chunks of <=100

    def fake_request(method, url, auth=None, timeout=None, **kwargs):
        n_create = len(kwargs["json"].get("create", []))
        skus = [item["sku"] for item in kwargs["json"]["create"]]
        if skus[0] == "S100":  # fail the 2nd chunk only (400 = no retry, no sleep)
            return _mock_response({}, status_code=400)
        return _mock_response({"create": [{"sku": s} for s in skus]})

    with patch("requests.request", side_effect=fake_request):
        result = client.batch_products(create=create_items, chunk_size=100)

    succeeded = [r["sku"] for r in result["create"] if "error" not in r]
    failed = [r["sku"] for r in result["create"] if "error" in r]
    assert len(succeeded) == 150  # chunks 1 and 3
    assert len(failed) == 100     # chunk 2
    assert "S100" in failed
    assert "S0" in succeeded and "S200" in succeeded


def test_batch_products_uses_extended_timeout():
    client = WooCommerceClient("https://example.com", "ck", "cs")
    seen_timeouts = []

    def fake_request(method, url, auth=None, timeout=None, **kwargs):
        seen_timeouts.append(timeout)
        return _mock_response({"create": []})

    with patch("requests.request", side_effect=fake_request):
        client.batch_products(create=[{"sku": "S1"}])

    from app.sync.woocommerce_client import BATCH_TIMEOUT_SECONDS
    assert seen_timeouts == [BATCH_TIMEOUT_SECONDS]


def test_ping_succeeds_on_200():
    client = WooCommerceClient("https://example.com", "ck", "cs")

    def fake_request(method, url, auth=None, timeout=None, **kwargs):
        assert method == "GET"
        assert kwargs["params"]["per_page"] == 1
        return _mock_response([{"id": 1}])

    with patch("requests.request", side_effect=fake_request):
        client.ping()  # must not raise


def test_ping_raises_on_auth_failure():
    client = WooCommerceClient("https://example.com", "bad-key", "bad-secret")

    def fake_request(method, url, auth=None, timeout=None, **kwargs):
        return _mock_response({"message": "Invalid signature"}, status_code=401)

    with patch("requests.request", side_effect=fake_request):
        with pytest.raises(WooCommerceError):
            client.ping()


def test_upload_media_requires_wp_auth():
    client = WooCommerceClient("https://example.com", "ck", "cs")
    with pytest.raises(WooCommerceError):
        client.upload_media(b"data", "f.png", "image/png")


def test_upload_media_success():
    client = WooCommerceClient("https://example.com", "ck", "cs",
                                wp_username="admin", wp_app_password="app-pass")

    def fake_request(method, url, auth=None, timeout=None, **kwargs):
        assert auth == ("admin", "app-pass")
        return _mock_response({"id": 777})

    with patch("requests.request", side_effect=fake_request):
        media_id = client.upload_media(b"data", "f.png", "image/png")
    assert media_id == 777


def test_fetch_all_products_requests_status_any():
    # WooCommerce's REST API defaults an un-filtered /products list to
    # published items only -- must explicitly ask for every status so
    # draft/pending/private products aren't silently skipped.
    client = WooCommerceClient("https://example.com", "ck", "cs")
    calls = []

    def fake_request(method, url, auth=None, timeout=None, **kwargs):
        calls.append(kwargs.get("params"))
        return _mock_response([])

    with patch("requests.request", side_effect=fake_request):
        result = client.fetch_all_products()

    assert result == []
    assert calls[0]["status"] == "any"


def test_fetch_all_products_paginates():
    client = WooCommerceClient("https://example.com", "ck", "cs")
    pages = [[{"id": i} for i in range(100)], [{"id": 100}]]

    def fake_request(method, url, auth=None, timeout=None, **kwargs):
        page = kwargs["params"]["page"]
        return _mock_response(pages[page - 1])

    with patch("requests.request", side_effect=fake_request):
        result = client.fetch_all_products()

    assert len(result) == 101


