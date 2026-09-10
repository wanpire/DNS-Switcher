import httpx
import pytest_asyncio
import respx
from httpx import ASGITransport, AsyncClient

from app.api.deps import get_cloudflare_client
from app.cloudflare.client import CloudflareClient
from app.db.session import get_db
from app.main import app
from app.models import Datacenter, DnsTarget, Domain, RecordType, SwitchGroup, SwitchGroupMember

BASE = "https://api.cloudflare.com/client/v4"
# Matches INTERNAL_API_SHARED_SECRET as set in the test run's environment.
SECRET = "test"


def _cf_ok(result):
    return httpx.Response(200, json={"success": True, "result": result})


async def _make_domain(session, name="example.com", zone="zone-1"):
    domain = Domain(name=name, cloudflare_zone_id=zone)
    session.add(domain)
    await session.flush()
    return domain


async def _make_datacenter(session, name, ip):
    dc = Datacenter(name=name, ip_address=ip)
    session.add(dc)
    await session.flush()
    return dc


async def _make_target(session, domain, dc=None, name="www", cf_record_id="cf-1"):
    target = DnsTarget(
        domain_id=domain.id,
        name=name,
        record_type=RecordType.A,
        cloudflare_record_id=cf_record_id,
        current_datacenter_id=dc.id if dc else None,
    )
    session.add(target)
    await session.flush()
    return target


@pytest_asyncio.fixture
async def cf_client():
    client = CloudflareClient(api_token="test-token")
    yield client
    await client.aclose()


@pytest_asyncio.fixture
async def api_client(session, cf_client):
    async def _override_get_db():
        yield session

    app.dependency_overrides[get_db] = _override_get_db
    app.dependency_overrides[get_cloudflare_client] = lambda: cf_client
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        yield client
    app.dependency_overrides.clear()


async def test_missing_secret_header_returns_401(api_client):
    response = await api_client.get("/targets")
    assert response.status_code == 401


async def test_wrong_secret_header_returns_401(api_client):
    response = await api_client.get("/targets", headers={"X-Internal-Secret": "wrong"})
    assert response.status_code == 401


async def test_list_targets_groups_datacenters(session, api_client):
    domain = await _make_domain(session)
    dc = await _make_datacenter(session, "dc1", "1.1.1.1")
    target = await _make_target(session, domain, dc)
    group = SwitchGroup(name="group1")
    session.add(group)
    await session.flush()
    session.add(SwitchGroupMember(switch_group_id=group.id, dns_target_id=target.id, position=0))
    await session.flush()

    headers = {"X-Internal-Secret": SECRET}

    targets_resp = await api_client.get("/targets", headers=headers)
    assert targets_resp.status_code == 200
    assert targets_resp.json()[0]["id"] == target.id
    assert targets_resp.json()[0]["record_type"] == "A"

    groups_resp = await api_client.get("/groups", headers=headers)
    assert groups_resp.status_code == 200
    assert groups_resp.json()[0]["member_dns_target_ids"] == [target.id]

    dc_resp = await api_client.get("/datacenters", headers=headers)
    assert dc_resp.status_code == 200
    assert dc_resp.json()[0]["ip_address"] == "1.1.1.1"
    assert dc_resp.json()[0]["status"] == "active"


@respx.mock
async def test_plan_and_execute_single_switch_via_api(session, api_client):
    domain = await _make_domain(session)
    dc1 = await _make_datacenter(session, "dc1", "1.1.1.1")
    dc2 = await _make_datacenter(session, "dc2", "2.2.2.2")
    target = await _make_target(session, domain, dc1)

    headers = {"X-Internal-Secret": SECRET}

    plan_resp = await api_client.post(
        "/switch/single/plan",
        json={"dns_target_id": target.id, "target_datacenter_id": dc2.id},
        headers=headers,
    )
    assert plan_resp.status_code == 200
    body = plan_resp.json()
    assert body["current_content"] == "1.1.1.1"
    assert body["new_content"] == "2.2.2.2"
    assert body["no_op"] is False

    respx.get(f"{BASE}/zones/zone-1/dns_records/cf-1").mock(
        return_value=_cf_ok({"id": "cf-1", "content": "1.1.1.1"})
    )
    respx.patch(f"{BASE}/zones/zone-1/dns_records/cf-1").mock(
        return_value=_cf_ok({"id": "cf-1", "content": "2.2.2.2"})
    )

    exec_resp = await api_client.post(
        "/switch/single/execute",
        json={"dns_target_id": target.id, "target_datacenter_id": dc2.id, "actor": "alice"},
        headers=headers,
    )
    assert exec_resp.status_code == 200
    exec_body = exec_resp.json()
    assert exec_body["success"] is True
    assert exec_body["skipped"] is False
    assert exec_body["audit_log_entry_id"] is not None

    await session.refresh(target)
    assert target.current_datacenter_id == dc2.id


async def test_plan_single_switch_unknown_target_returns_400(session, api_client):
    dc = await _make_datacenter(session, "dc1", "1.1.1.1")
    headers = {"X-Internal-Secret": SECRET}
    resp = await api_client.post(
        "/switch/single/plan",
        json={"dns_target_id": 999999, "target_datacenter_id": dc.id},
        headers=headers,
    )
    assert resp.status_code == 400


@respx.mock
async def test_rollback_via_api(session, api_client):
    domain = await _make_domain(session)
    dc1 = await _make_datacenter(session, "dc1", "1.1.1.1")
    dc2 = await _make_datacenter(session, "dc2", "2.2.2.2")
    target = await _make_target(session, domain, dc1)

    headers = {"X-Internal-Secret": SECRET}

    respx.get(f"{BASE}/zones/zone-1/dns_records/cf-1").mock(
        side_effect=[
            _cf_ok({"id": "cf-1", "content": "1.1.1.1"}),
            _cf_ok({"id": "cf-1", "content": "2.2.2.2"}),
        ]
    )
    respx.patch(f"{BASE}/zones/zone-1/dns_records/cf-1").mock(
        side_effect=[
            _cf_ok({"id": "cf-1", "content": "2.2.2.2"}),
            _cf_ok({"id": "cf-1", "content": "1.1.1.1"}),
        ]
    )

    exec_resp = await api_client.post(
        "/switch/single/execute",
        json={"dns_target_id": target.id, "target_datacenter_id": dc2.id, "actor": "alice"},
        headers=headers,
    )
    entry_id = exec_resp.json()["audit_log_entry_id"]

    rollback_resp = await api_client.post(
        f"/rollback/{entry_id}",
        json={"actor": "bob"},
        headers=headers,
    )
    assert rollback_resp.status_code == 200
    assert rollback_resp.json()["success"] is True

    await session.refresh(target)
    assert target.current_datacenter_id == dc1.id


@respx.mock
async def test_audit_log_pagination_and_filtering(session, api_client):
    domain = await _make_domain(session)
    dc1 = await _make_datacenter(session, "dc1", "1.1.1.1")
    dc2 = await _make_datacenter(session, "dc2", "2.2.2.2")
    target = await _make_target(session, domain, dc1)

    headers = {"X-Internal-Secret": SECRET}

    respx.get(f"{BASE}/zones/zone-1/dns_records/cf-1").mock(
        return_value=_cf_ok({"id": "cf-1", "content": "1.1.1.1"})
    )
    respx.patch(f"{BASE}/zones/zone-1/dns_records/cf-1").mock(
        return_value=_cf_ok({"id": "cf-1", "content": "2.2.2.2"})
    )

    await api_client.post(
        "/switch/single/execute",
        json={"dns_target_id": target.id, "target_datacenter_id": dc2.id, "actor": "alice"},
        headers=headers,
    )

    log_resp = await api_client.get(
        "/audit-log", params={"dns_target_id": target.id}, headers=headers
    )
    assert log_resp.status_code == 200
    body = log_resp.json()
    assert body["total"] == 1
    assert body["items"][0]["actor"] == "alice"
    assert body["items"][0]["status"] == "success"

    empty_resp = await api_client.get(
        "/audit-log", params={"dns_target_id": 999999}, headers=headers
    )
    assert empty_resp.json()["total"] == 0
