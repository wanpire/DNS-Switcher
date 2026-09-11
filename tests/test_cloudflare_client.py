import httpx
import pytest
import pytest_asyncio
import respx

from app.cloudflare.client import MAX_ATTEMPTS, CloudflareApiError, CloudflareClient

BASE = "https://api.cloudflare.com/client/v4"


@pytest_asyncio.fixture
async def client():
    c = CloudflareClient(api_token="test-token")
    yield c
    await c.aclose()


@respx.mock
async def test_list_dns_records_success(client):
    route = respx.get(f"{BASE}/zones/zone-1/dns_records").mock(
        return_value=httpx.Response(
            200,
            json={
                "success": True,
                "result": [
                    {"id": "rec1", "name": "www.example.com", "type": "A", "content": "1.2.3.4"}
                ],
            },
        )
    )
    records = await client.list_dns_records("zone-1")
    assert records == [
        {"id": "rec1", "name": "www.example.com", "type": "A", "content": "1.2.3.4"}
    ]
    assert route.called


@respx.mock
async def test_list_dns_records_paginates(client):
    page1 = [{"id": f"rec{i}", "name": "x", "type": "A", "content": "1.1.1.1"} for i in range(100)]
    page2 = [{"id": "rec100", "name": "x", "type": "A", "content": "1.1.1.1"}]

    def responder(request):
        page = request.url.params.get("page")
        body = page1 if page == "1" else page2
        return httpx.Response(200, json={"success": True, "result": body})

    respx.get(f"{BASE}/zones/zone-1/dns_records").mock(side_effect=responder)
    records = await client.list_dns_records("zone-1")
    assert len(records) == 101


@respx.mock
async def test_get_dns_record_success(client):
    respx.get(f"{BASE}/zones/zone-1/dns_records/rec1").mock(
        return_value=httpx.Response(200, json={"success": True, "result": {"id": "rec1"}})
    )
    record = await client.get_dns_record("zone-1", "rec1")
    assert record == {"id": "rec1"}


@respx.mock
async def test_update_dns_record_success(client):
    respx.patch(f"{BASE}/zones/zone-1/dns_records/rec1").mock(
        return_value=httpx.Response(
            200, json={"success": True, "result": {"id": "rec1", "content": "5.6.7.8"}}
        )
    )
    record = await client.update_dns_record("zone-1", "rec1", content="5.6.7.8", proxied=False)
    assert record["content"] == "5.6.7.8"


@respx.mock
async def test_create_dns_record_success(client):
    respx.post(f"{BASE}/zones/zone-1/dns_records").mock(
        return_value=httpx.Response(201, json={"success": True, "result": {"id": "rec2"}})
    )
    record = await client.create_dns_record(
        "zone-1", name="new.example.com", type="A", content="9.9.9.9", proxied=True
    )
    assert record == {"id": "rec2"}


@respx.mock
async def test_delete_dns_record_success(client):
    respx.delete(f"{BASE}/zones/zone-1/dns_records/rec1").mock(
        return_value=httpx.Response(200, json={"success": True, "result": {"id": "rec1"}})
    )
    record = await client.delete_dns_record("zone-1", "rec1")
    assert record == {"id": "rec1"}


@respx.mock
async def test_retries_on_429_then_succeeds(client):
    route = respx.get(f"{BASE}/zones/zone-1/dns_records/rec1").mock(
        side_effect=[
            httpx.Response(
                429,
                headers={"Retry-After": "0"},
                json={"success": False, "errors": [{"message": "rate limited"}]},
            ),
            httpx.Response(200, json={"success": True, "result": {"id": "rec1"}}),
        ]
    )
    record = await client.get_dns_record("zone-1", "rec1")
    assert record == {"id": "rec1"}
    assert route.call_count == 2


@respx.mock
async def test_retries_on_5xx_then_succeeds(client):
    route = respx.get(f"{BASE}/zones/zone-1/dns_records/rec1").mock(
        side_effect=[
            httpx.Response(503, json={"success": False, "errors": []}),
            httpx.Response(200, json={"success": True, "result": {"id": "rec1"}}),
        ]
    )
    record = await client.get_dns_record("zone-1", "rec1")
    assert record == {"id": "rec1"}
    assert route.call_count == 2


@respx.mock
async def test_permanent_failure_raises_typed_error_without_retry(client):
    route = respx.get(f"{BASE}/zones/zone-1/dns_records/rec1").mock(
        return_value=httpx.Response(
            400, json={"success": False, "errors": [{"code": 1003, "message": "Invalid zone"}]}
        )
    )
    with pytest.raises(CloudflareApiError) as exc_info:
        await client.get_dns_record("zone-1", "rec1")
    assert exc_info.value.status_code == 400
    assert exc_info.value.errors == [{"code": 1003, "message": "Invalid zone"}]
    assert route.call_count == 1


@respx.mock
async def test_exhausts_retries_raises_typed_error(client):
    route = respx.get(f"{BASE}/zones/zone-1/dns_records/rec1").mock(
        return_value=httpx.Response(
            429,
            headers={"Retry-After": "0"},
            json={"success": False, "errors": [{"message": "rate limited"}]},
        )
    )
    with pytest.raises(CloudflareApiError) as exc_info:
        await client.get_dns_record("zone-1", "rec1")
    assert exc_info.value.status_code == 429
    assert route.call_count == MAX_ATTEMPTS
