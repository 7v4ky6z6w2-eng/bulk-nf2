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


def test_find_or_create_category_uses_cache():
    client = WooCommerceClient("https://example.com", "ck", "cs")
    calls = []

    def fake_request(method, url, auth=None, timeout=None, **kwargs):
        calls.append((method, url))
        if method == "GET":
            return _mock_response([{"id": 5, "name": "Scolaire"}])
        return _mock_response({"id": 99, "name": kwargs["json"]["name"]})

    with patch("requests.request", side_effect=fake_request):
        cat_id = client.find_or_create_category("Scolaire")
        assert cat_id == 5
        # second call should hit the cache, no extra GET/POST
        n_calls_before = len(calls)
        cat_id_again = client.find_or_create_category("scolaire")
        assert cat_id_again == 5
        assert len(calls) == n_calls_before


def test_find_or_create_category_creates_when_missing():
    client = WooCommerceClient("https://example.com", "ck", "cs")

    def fake_request(method, url, auth=None, timeout=None, **kwargs):
        if method == "GET":
            return _mock_response([])
        return _mock_response({"id": 42, "name": kwargs["json"]["name"]})

    with patch("requests.request", side_effect=fake_request):
        cat_id = client.find_or_create_category("Bureau")
        assert cat_id == 42


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


def test_request_raises_on_4xx():
    client = WooCommerceClient("https://example.com", "ck", "cs")

    def fake_request(method, url, auth=None, timeout=None, **kwargs):
        return _mock_response({}, status_code=404)

    with patch("requests.request", side_effect=fake_request):
        with pytest.raises(WooCommerceError):
            client.find_or_create_category("Anything")
