"""Tests for the BIHR V3 API client (mocked HTTP only, no real network)."""

import httpx
import pytest
import respx

from bihr_connector.bihr_client import BihrApiError, BihrClient

BASE_URL = "https://api.bihr.test"


def make_client(**kwargs):
    kwargs.setdefault("min_interval_seconds", 0.0)
    return BihrClient(BASE_URL, "user", "pass", **kwargs)


def token_response(access_token="tok-1", expires_in=1799):
    return httpx.Response(
        200,
        json={"access_token": access_token, "token_type": "bearer", "expires_in": expires_in},
    )


# ---------------------------------------------------------------------------
# authenticate()
# ---------------------------------------------------------------------------


def test_authenticate_uses_form_urlencoded_not_multipart(respx_mock):
    route = respx_mock.post(f"{BASE_URL}/api/v3/Authentication/Token").mock(
        return_value=token_response()
    )
    client = make_client()

    client.authenticate()

    assert route.called
    sent_request = route.calls.last.request
    content_type = sent_request.headers.get("content-type", "")
    assert content_type.startswith("application/x-www-form-urlencoded")
    assert content_type.strip() != "multipart/form-data"
    assert "multipart" not in content_type
    body = sent_request.content.decode()
    assert "UserName=user" in body
    assert "PassWord=pass" in body


def test_authenticate_sets_token(respx_mock):
    respx_mock.post(f"{BASE_URL}/api/v3/Authentication/Token").mock(
        return_value=token_response(access_token="secret-token")
    )
    client = make_client()

    client.authenticate()

    assert client._token == "secret-token"


def test_authenticate_accepts_accesstoken_key_variant(respx_mock):
    respx_mock.post(f"{BASE_URL}/api/v3/Authentication/Token").mock(
        return_value=httpx.Response(200, json={"accessToken": "alt-token", "expiresIn": 1799})
    )
    client = make_client()

    client.authenticate()

    assert client._token == "alt-token"


def test_authenticate_reuses_cached_token_without_force(respx_mock):
    route = respx_mock.post(f"{BASE_URL}/api/v3/Authentication/Token").mock(
        return_value=token_response()
    )
    client = make_client()

    client.authenticate()
    client.authenticate()

    assert route.call_count == 1


def test_authenticate_force_requests_new_token(respx_mock):
    route = respx_mock.post(f"{BASE_URL}/api/v3/Authentication/Token").mock(
        return_value=token_response()
    )
    client = make_client()

    client.authenticate()
    client.authenticate(force=True)

    assert route.call_count == 2


def test_authenticate_raises_bihr_api_error_on_http_error(respx_mock):
    respx_mock.post(f"{BASE_URL}/api/v3/Authentication/Token").mock(
        return_value=httpx.Response(401, text="bad credentials")
    )
    client = make_client()

    with pytest.raises(BihrApiError):
        client.authenticate()


def test_authenticate_raises_when_no_token_in_response(respx_mock):
    respx_mock.post(f"{BASE_URL}/api/v3/Authentication/Token").mock(
        return_value=httpx.Response(200, json={"expires_in": 1799})
    )
    client = make_client()

    with pytest.raises(BihrApiError):
        client.authenticate()


# ---------------------------------------------------------------------------
# retry-with-reauth on 401
# ---------------------------------------------------------------------------


def test_authenticate_retries_once_on_401(respx_mock):
    auth_route = respx_mock.post(f"{BASE_URL}/api/v3/Authentication/Token").mock(
        side_effect=[token_response(access_token="tok-1"), token_response(access_token="tok-2")]
    )
    catalog_route = respx_mock.post(f"{BASE_URL}/api/v3/Catalogs/Request").mock(
        side_effect=[
            httpx.Response(401, text="token expired"),
            httpx.Response(200, json={"requestId": "req-123", "status": "Pending"}),
        ]
    )
    client = make_client()

    result = client.request_catalog("Products")

    assert result == {"requestId": "req-123", "status": "Pending"}
    assert auth_route.call_count == 2
    assert catalog_route.call_count == 2
    # second attempt must carry the freshly re-authenticated token
    second_call_headers = catalog_route.calls[1].request.headers
    assert second_call_headers["authorization"] == "Bearer tok-2"


def test_request_raises_after_second_401(respx_mock):
    respx_mock.post(f"{BASE_URL}/api/v3/Authentication/Token").mock(
        return_value=token_response()
    )
    respx_mock.post(f"{BASE_URL}/api/v3/Catalogs/Request").mock(
        return_value=httpx.Response(401, text="still unauthorized")
    )
    client = make_client()

    with pytest.raises(BihrApiError):
        client.request_catalog("Products")


# ---------------------------------------------------------------------------
# request_catalog()
# ---------------------------------------------------------------------------


def test_request_catalog_sends_expected_body(respx_mock):
    respx_mock.post(f"{BASE_URL}/api/v3/Authentication/Token").mock(
        return_value=token_response()
    )
    route = respx_mock.post(f"{BASE_URL}/api/v3/Catalogs/Request").mock(
        return_value=httpx.Response(200, json={"requestId": "req-1", "status": "Pending"})
    )
    client = make_client()

    result = client.request_catalog("Prices", language="fr")

    assert result["requestId"] == "req-1"
    sent = route.calls.last.request
    assert sent.headers["authorization"].startswith("Bearer ")
    import json as _json

    body = _json.loads(sent.content)
    assert body == {
        "catalogType": "Prices",
        "language": "fr",
        "compressionType": "ZIP",
        "serializationType": "JSON",
    }


# ---------------------------------------------------------------------------
# wait_catalog()
# ---------------------------------------------------------------------------


def test_wait_catalog_returns_once_finished(respx_mock):
    respx_mock.post(f"{BASE_URL}/api/v3/Authentication/Token").mock(
        return_value=token_response()
    )
    respx_mock.get(f"{BASE_URL}/api/v3/Catalogs/Request/req-1").mock(
        side_effect=[
            httpx.Response(200, json={"status": "Pending"}),
            httpx.Response(200, json={"status": "Finished", "catalogFileId": "file-99"}),
        ]
    )
    client = make_client()

    result = client.wait_catalog("req-1", poll_seconds=0.0)

    assert result["status"] == "Finished"
    assert result["catalogFileId"] == "file-99"


def test_wait_catalog_raises_on_failed_status(respx_mock):
    respx_mock.post(f"{BASE_URL}/api/v3/Authentication/Token").mock(
        return_value=token_response()
    )
    respx_mock.get(f"{BASE_URL}/api/v3/Catalogs/Request/req-1").mock(
        return_value=httpx.Response(200, json={"status": "Failed", "errorMessage": "boom"})
    )
    client = make_client()

    with pytest.raises(BihrApiError):
        client.wait_catalog("req-1", poll_seconds=0.0)


def test_wait_catalog_raises_on_cancelled_status(respx_mock):
    respx_mock.post(f"{BASE_URL}/api/v3/Authentication/Token").mock(
        return_value=token_response()
    )
    respx_mock.get(f"{BASE_URL}/api/v3/Catalogs/Request/req-1").mock(
        return_value=httpx.Response(200, json={"status": "Cancelled"})
    )
    client = make_client()

    with pytest.raises(BihrApiError):
        client.wait_catalog("req-1", poll_seconds=0.0)


def test_wait_catalog_raises_on_timeout(respx_mock):
    respx_mock.post(f"{BASE_URL}/api/v3/Authentication/Token").mock(
        return_value=token_response()
    )
    respx_mock.get(f"{BASE_URL}/api/v3/Catalogs/Request/req-1").mock(
        return_value=httpx.Response(200, json={"status": "Pending"})
    )
    client = make_client()

    with pytest.raises(BihrApiError):
        client.wait_catalog("req-1", poll_seconds=0.0, max_wait_minutes=0.0)


def test_wait_catalog_raises_when_finished_without_file_id(respx_mock):
    respx_mock.post(f"{BASE_URL}/api/v3/Authentication/Token").mock(
        return_value=token_response()
    )
    respx_mock.get(f"{BASE_URL}/api/v3/Catalogs/Request/req-1").mock(
        return_value=httpx.Response(200, json={"status": "Finished"})
    )
    client = make_client()

    with pytest.raises(BihrApiError):
        client.wait_catalog("req-1", poll_seconds=0.0)


# ---------------------------------------------------------------------------
# download_catalog_file()
# ---------------------------------------------------------------------------


def test_download_catalog_file_returns_raw_bytes(respx_mock):
    respx_mock.post(f"{BASE_URL}/api/v3/Authentication/Token").mock(
        return_value=token_response()
    )
    zip_bytes = b"PK\x03\x04fakezipcontent"
    respx_mock.get(f"{BASE_URL}/api/v3/Catalogs/File/file-99").mock(
        return_value=httpx.Response(
            200, content=zip_bytes, headers={"Content-Type": "application/zip"}
        )
    )
    client = make_client()

    result = client.download_catalog_file("file-99")

    assert result == zip_bytes
    assert isinstance(result, bytes)


# ---------------------------------------------------------------------------
# part_numbers_for_brand()
# ---------------------------------------------------------------------------


def test_part_numbers_for_brand_dedupes_across_pages(respx_mock):
    respx_mock.post(f"{BASE_URL}/api/v3/Authentication/Token").mock(
        return_value=token_response()
    )
    # Pages must be full-sized (MAX_BATCH=500) so the "last page" heuristic
    # (page length < page size means no more pages) doesn't stop early.
    page_1 = [{"partNumber": "AAA"}] + [{"partNumber": f"FILLER-{i}"} for i in range(498)] + [
        {"partNumber": "BBB"}
    ]
    page_2 = [{"partNumber": "BBB"}] + [{"partNumber": "CCC"}]  # overlap + short page -> stop
    respx_mock.post(f"{BASE_URL}/api/v3/Products/PartNumbers").mock(
        side_effect=[
            httpx.Response(200, json={"productCriteria": page_1}),
            httpx.Response(200, json={"productCriteria": page_2}),
        ]
    )
    client = make_client()

    result = client.part_numbers_for_brand("BREMBO")

    assert result[0] == "AAA"
    assert result[-2:] == ["BBB", "CCC"]
    assert len(result) == len(set(result))  # no duplicates despite BBB appearing on both pages


def test_part_numbers_for_brand_stops_on_short_page(respx_mock):
    respx_mock.post(f"{BASE_URL}/api/v3/Authentication/Token").mock(
        return_value=token_response()
    )
    route = respx_mock.post(f"{BASE_URL}/api/v3/Products/PartNumbers").mock(
        return_value=httpx.Response(
            200, json={"productCriteria": [{"partNumber": "AAA"}]}
        )
    )
    client = make_client()

    result = client.part_numbers_for_brand("BREMBO")

    assert result == ["AAA"]
    assert route.call_count == 1


def test_part_numbers_for_brand_sends_brand_and_pagination(respx_mock):
    respx_mock.post(f"{BASE_URL}/api/v3/Authentication/Token").mock(
        return_value=token_response()
    )
    route = respx_mock.post(f"{BASE_URL}/api/v3/Products/PartNumbers").mock(
        return_value=httpx.Response(200, json={"productCriteria": []})
    )
    client = make_client()

    client.part_numbers_for_brand("BREMBO")

    import json as _json

    sent_body = _json.loads(route.calls.last.request.content)
    assert sent_body["brandNames"] == ["BREMBO"]
    assert sent_body["pageNumber"] == 0
    assert sent_body["listSize"] == 500


# ---------------------------------------------------------------------------
# prices_for_parts()
# ---------------------------------------------------------------------------


def test_prices_for_parts_batches_by_500(respx_mock):
    respx_mock.post(f"{BASE_URL}/api/v3/Authentication/Token").mock(
        return_value=token_response()
    )
    part_numbers = [f"P{i}" for i in range(750)]

    call_batches = []

    def _responder(request):
        import json as _json

        payload = _json.loads(request.content)
        batch = payload["productCriteria"]
        call_batches.append(len(batch))
        return httpx.Response(
            200,
            json=[{"partNumber": item["partNumber"], "dealerPriceExclTaxes": 1.0} for item in batch],
        )

    respx_mock.post(f"{BASE_URL}/api/v3/Products/Prices").mock(side_effect=_responder)

    client = make_client()
    result = client.prices_for_parts(part_numbers)

    assert call_batches == [500, 250]
    assert len(result) == 750


def test_prices_for_parts_empty_input_makes_no_request(respx_mock):
    route = respx_mock.post(f"{BASE_URL}/api/v3/Products/Prices").mock(
        return_value=httpx.Response(200, json=[])
    )
    client = make_client()

    result = client.prices_for_parts([])

    assert result == []
    assert route.call_count == 0


# ---------------------------------------------------------------------------
# throttle
# ---------------------------------------------------------------------------


def test_min_interval_seconds_throttles_between_requests(respx_mock, monkeypatch):
    respx_mock.post(f"{BASE_URL}/api/v3/Authentication/Token").mock(
        return_value=token_response()
    )
    respx_mock.post(f"{BASE_URL}/api/v3/Products/Prices").mock(
        return_value=httpx.Response(200, json=[])
    )

    sleep_calls = []
    monkeypatch.setattr(
        "bihr_connector.bihr_client.time.sleep", lambda s: sleep_calls.append(s)
    )
    # Force "no time elapsed" between calls so the throttle always has to wait.
    monotonic_values = iter([0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0])
    monkeypatch.setattr(
        "bihr_connector.bihr_client.time.monotonic", lambda: next(monotonic_values, 0.0)
    )

    client = BihrClient(BASE_URL, "user", "pass", min_interval_seconds=0.6)
    client.prices_for_parts(["AAA"])
    client.prices_for_parts(["BBB"])

    assert any(s > 0 for s in sleep_calls)
