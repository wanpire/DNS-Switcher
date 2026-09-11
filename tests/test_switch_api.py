import httpx
import pytest_asyncio
import respx
from httpx import ASGITransport, AsyncClient

from app.api.deps import get_cloudflare_client
from app.cloudflare.client import CloudflareClient
from app.db.session import get_db
from app.main import app
from app.models import Datacenter, DnsTarget, Domain, RecordType, SwitchGroup, SwitchGroupMember, TargetDatacenterIp

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


async def _make_datacenter(session, name):
    dc = Datacenter(name=name)
    session.add(dc)
    await session.flush()
    return dc


async def _make_target(session, domain, name="www"):
    target = DnsTarget(domain_id=domain.id, name=name, record_type=RecordType.A)
    session.add(target)
    await session.flush()
    return target


async def _add_ip(session, target, dc, ip, record_id=None):
    session.add(
        TargetDatacenterIp(
            dns_target_id=target.id, datacenter_id=dc.id, slot_index=0, ip_address=ip, cloudflare_record_id=record_id
        )
    )
    await session.flush()


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
    dc = await _make_datacenter(session, "dc1")
    target = await _make_target(session, domain)
    await _add_ip(session, target, dc, "1.1.1.1", "cf-1")
    target.current_datacenter_id = dc.id
    await session.flush()
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
    assert dc_resp.json()[0]["status"] == "active"

    domains_resp = await api_client.get("/domains", headers=headers)
    assert domains_resp.status_code == 200
    assert domains_resp.json()[0]["name"] == "example.com"


@respx.mock
async def test_plan_and_execute_single_switch_via_api(session, api_client):
    domain = await _make_domain(session)
    dc1 = await _make_datacenter(session, "dc1")
    dc2 = await _make_datacenter(session, "dc2")
    target = await _make_target(session, domain)
    await _add_ip(session, target, dc1, "1.1.1.1", "cf-1")
    await _add_ip(session, target, dc2, "2.2.2.2")
    target.current_datacenter_id = dc1.id
    await session.flush()

    headers = {"X-Internal-Secret": SECRET}

    plan_resp = await api_client.post(
        "/switch/single/plan",
        json={"dns_target_id": target.id, "target_datacenter_id": dc2.id},
        headers=headers,
    )
    assert plan_resp.status_code == 200
    body = plan_resp.json()
    assert body["slot_diffs"] == [{"slot_index": 0, "current_ip": "1.1.1.1", "new_ip": "2.2.2.2"}]
    assert body["no_op"] is False
    assert body["slot_count_mismatch"] is False

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
    assert exec_body["slot_results"][0]["action"] == "updated"

    await session.refresh(target)
    assert target.current_datacenter_id == dc2.id


async def test_plan_single_switch_unknown_target_returns_400(session, api_client):
    dc = await _make_datacenter(session, "dc1")
    headers = {"X-Internal-Secret": SECRET}
    resp = await api_client.post(
        "/switch/single/plan",
        json={"dns_target_id": 999999, "target_datacenter_id": dc.id},
        headers=headers,
    )
    assert resp.status_code == 400


async def test_execute_single_switch_slot_mismatch_requires_confirmation(session, api_client):
    domain = await _make_domain(session)
    dc1 = await _make_datacenter(session, "dc1")
    dc2 = await _make_datacenter(session, "dc2")
    target = await _make_target(session, domain)
    await _add_ip(session, target, dc1, "1.1.1.1", "cf-1")
    session.add_all(
        [
            TargetDatacenterIp(dns_target_id=target.id, datacenter_id=dc2.id, slot_index=0, ip_address="2.2.2.1"),
            TargetDatacenterIp(dns_target_id=target.id, datacenter_id=dc2.id, slot_index=1, ip_address="2.2.2.2"),
        ]
    )
    target.current_datacenter_id = dc1.id
    await session.flush()

    headers = {"X-Internal-Secret": SECRET}
    resp = await api_client.post(
        "/switch/single/execute",
        json={"dns_target_id": target.id, "target_datacenter_id": dc2.id, "actor": "alice"},
        headers=headers,
    )
    assert resp.status_code == 400
    assert "allow_slot_count_mismatch" in resp.json()["detail"]


@respx.mock
async def test_rollback_via_api(session, api_client):
    domain = await _make_domain(session)
    dc1 = await _make_datacenter(session, "dc1")
    dc2 = await _make_datacenter(session, "dc2")
    target = await _make_target(session, domain)
    await _add_ip(session, target, dc1, "1.1.1.1", "cf-1")
    await _add_ip(session, target, dc2, "2.2.2.2")
    target.current_datacenter_id = dc1.id
    await session.flush()

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
    dc1 = await _make_datacenter(session, "dc1")
    dc2 = await _make_datacenter(session, "dc2")
    target = await _make_target(session, domain)
    await _add_ip(session, target, dc1, "1.1.1.1", "cf-1")
    await _add_ip(session, target, dc2, "2.2.2.2")
    target.current_datacenter_id = dc1.id
    await session.flush()

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


async def test_plan_and_execute_single_switch_unavailable_destination(session, api_client):
    # A target with no IPs configured for the destination datacenter must
    # come back as a normal 200 with unavailable=True -- never a raw error.
    domain = await _make_domain(session)
    dc1 = await _make_datacenter(session, "dc1")
    dc2 = await _make_datacenter(session, "dc2")
    target = await _make_target(session, domain)
    await _add_ip(session, target, dc1, "1.1.1.1", "cf-1")
    target.current_datacenter_id = dc1.id
    await session.flush()

    headers = {"X-Internal-Secret": SECRET}

    plan_resp = await api_client.post(
        "/switch/single/plan",
        json={"dns_target_id": target.id, "target_datacenter_id": dc2.id},
        headers=headers,
    )
    assert plan_resp.status_code == 200
    body = plan_resp.json()
    assert body["unavailable"] is True
    assert body["unavailable_reason"]
    assert body["slot_diffs"] == []

    exec_resp = await api_client.post(
        "/switch/single/execute",
        json={"dns_target_id": target.id, "target_datacenter_id": dc2.id, "actor": "alice"},
        headers=headers,
    )
    assert exec_resp.status_code == 200
    exec_body = exec_resp.json()
    assert exec_body["success"] is False
    assert exec_body["unavailable"] is True
    assert exec_body["audit_log_entry_id"] is None

    await session.refresh(target)
    assert target.current_datacenter_id == dc1.id  # untouched


@respx.mock
async def test_bulk_switch_via_api_with_domain_scoping_and_skipped_count(session, api_client):
    domain_a = await _make_domain(session, name="a.example.com", zone="zone-a")
    domain_b = await _make_domain(session, name="b.example.com", zone="zone-b")
    dc1 = await _make_datacenter(session, "dc1")
    dc2 = await _make_datacenter(session, "dc2")

    ready = await _make_target(session, domain_a, name="ready")
    await _add_ip(session, ready, dc1, "1.1.1.1", "cf-ready")
    await _add_ip(session, ready, dc2, "2.2.2.1")
    ready.current_datacenter_id = dc1.id

    not_deployed = await _make_target(session, domain_a, name="not-deployed")
    await _add_ip(session, not_deployed, dc1, "1.1.1.2", "cf-nd")
    not_deployed.current_datacenter_id = dc1.id

    other_domain = await _make_target(session, domain_b, name="other")
    await _add_ip(session, other_domain, dc1, "1.1.1.3", "cf-other")
    await _add_ip(session, other_domain, dc2, "2.2.2.3")
    other_domain.current_datacenter_id = dc1.id
    await session.flush()

    group = SwitchGroup(name="group1")
    session.add(group)
    await session.flush()
    session.add_all(
        [
            SwitchGroupMember(switch_group_id=group.id, dns_target_id=ready.id, position=0),
            SwitchGroupMember(switch_group_id=group.id, dns_target_id=not_deployed.id, position=1),
            SwitchGroupMember(switch_group_id=group.id, dns_target_id=other_domain.id, position=2),
        ]
    )
    await session.flush()

    headers = {"X-Internal-Secret": SECRET}

    plan_resp = await api_client.post(
        "/switch/bulk/plan",
        json={"switch_group_id": group.id, "target_datacenter_id": dc2.id, "domain_id": domain_a.id},
        headers=headers,
    )
    assert plan_resp.status_code == 200
    plan_ids = [p["dns_target_id"] for p in plan_resp.json()]
    assert set(plan_ids) == {ready.id, not_deployed.id}  # other_domain filtered out

    respx.get(f"{BASE}/zones/zone-a/dns_records/cf-ready").mock(
        return_value=_cf_ok({"id": "cf-ready", "content": "1.1.1.1"})
    )
    respx.patch(f"{BASE}/zones/zone-a/dns_records/cf-ready").mock(
        return_value=_cf_ok({"id": "cf-ready", "content": "2.2.2.1"})
    )

    exec_resp = await api_client.post(
        "/switch/bulk/execute",
        json={
            "switch_group_id": group.id,
            "target_datacenter_id": dc2.id,
            "actor": "alice",
            "domain_id": domain_a.id,
        },
        headers=headers,
    )
    assert exec_resp.status_code == 200
    body = exec_resp.json()
    assert body["succeeded_count"] == 1
    assert body["failed_count"] == 0
    assert body["skipped_count"] == 1
    result_ids = {r["dns_target_id"] for r in body["results"]}
    assert result_ids == {ready.id, not_deployed.id}

    await session.refresh(other_domain)
    assert other_domain.current_datacenter_id == dc1.id  # untouched -- filtered out by domain_id
