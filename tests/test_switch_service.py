from types import SimpleNamespace

import httpx
import pytest
import respx

from app.cloudflare.client import CloudflareClient
from app.models import AuditStatus, Datacenter, DnsTarget, Domain, RecordType, SwitchGroup, SwitchGroupMember
from app.services.switch_service import SwitchService, SwitchValidationError

BASE = "https://api.cloudflare.com/client/v4"


def _cf_ok(result):
    return httpx.Response(200, json={"success": True, "result": result})


def _cf_error(status_code, message="boom"):
    return httpx.Response(status_code, json={"success": False, "errors": [{"message": message}]})


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


@pytest.fixture(autouse=True)
def _no_bulk_delay(monkeypatch):
    monkeypatch.setattr(
        "app.services.switch_service.get_settings",
        lambda: SimpleNamespace(bulk_switch_delay_seconds=0),
    )


# --- plan_single_switch: pure DB diff, never calls Cloudflare -------------


async def test_plan_single_switch_never_calls_cloudflare(session):
    # No respx mock registered at all -- if the client tried to make an HTTP
    # call, respx would raise for the unmocked request.
    domain = await _make_domain(session)
    dc1 = await _make_datacenter(session, "dc1", "1.1.1.1")
    dc2 = await _make_datacenter(session, "dc2", "2.2.2.2")
    target = await _make_target(session, domain, dc1)

    client = CloudflareClient(api_token="test-token")
    plan = await SwitchService(session, client).plan_single_switch(target.id, dc2.id)

    assert plan.current_content == "1.1.1.1"
    assert plan.new_content == "2.2.2.2"
    assert plan.no_op is False
    await client.aclose()


async def test_plan_single_switch_no_op_when_already_on_target(session):
    domain = await _make_domain(session)
    dc1 = await _make_datacenter(session, "dc1", "1.1.1.1")
    target = await _make_target(session, domain, dc1)

    client = CloudflareClient(api_token="test-token")
    plan = await SwitchService(session, client).plan_single_switch(target.id, dc1.id)
    assert plan.no_op is True
    await client.aclose()


async def test_plan_single_switch_raises_when_not_synced(session):
    domain = await _make_domain(session)
    dc = await _make_datacenter(session, "dc1", "1.1.1.1")
    target = await _make_target(session, domain, dc, cf_record_id=None)

    client = CloudflareClient(api_token="test-token")
    with pytest.raises(SwitchValidationError, match="not been synced"):
        await SwitchService(session, client).plan_single_switch(target.id, dc.id)
    await client.aclose()


async def test_plan_single_switch_raises_when_datacenter_has_no_ip(session):
    domain = await _make_domain(session)
    dc_no_ip = Datacenter(name="dc-no-ip")
    session.add(dc_no_ip)
    await session.flush()
    target = await _make_target(session, domain)

    client = CloudflareClient(api_token="test-token")
    with pytest.raises(SwitchValidationError, match="no ip_address"):
        await SwitchService(session, client).plan_single_switch(target.id, dc_no_ip.id)
    await client.aclose()


# --- execute_single_switch --------------------------------------------------


@respx.mock
async def test_execute_single_switch_writes_and_logs_success(session):
    domain = await _make_domain(session)
    dc1 = await _make_datacenter(session, "dc1", "1.1.1.1")
    dc2 = await _make_datacenter(session, "dc2", "2.2.2.2")
    target = await _make_target(session, domain, dc1)

    respx.get(f"{BASE}/zones/zone-1/dns_records/cf-1").mock(
        return_value=_cf_ok({"id": "cf-1", "content": "1.1.1.1"})
    )
    update_route = respx.patch(f"{BASE}/zones/zone-1/dns_records/cf-1").mock(
        return_value=_cf_ok({"id": "cf-1", "content": "2.2.2.2"})
    )

    client = CloudflareClient(api_token="test-token")
    result = await SwitchService(session, client).execute_single_switch(
        target.id, dc2.id, actor="tester"
    )
    await client.aclose()

    assert result.success is True
    assert result.skipped is False
    assert update_route.called
    assert target.current_datacenter_id == dc2.id
    assert result.audit_log_entry.status == AuditStatus.success
    assert result.audit_log_entry.previous_datacenter_id == dc1.id
    assert result.audit_log_entry.new_datacenter_id == dc2.id


@respx.mock
async def test_execute_single_switch_skips_write_when_already_correct(session):
    domain = await _make_domain(session)
    dc1 = await _make_datacenter(session, "dc1", "1.1.1.1")
    dc2 = await _make_datacenter(session, "dc2", "2.2.2.2")
    # DB thinks it's still on dc1, but Cloudflare already shows dc2's IP.
    target = await _make_target(session, domain, dc1)

    respx.get(f"{BASE}/zones/zone-1/dns_records/cf-1").mock(
        return_value=_cf_ok({"id": "cf-1", "content": "2.2.2.2"})
    )
    update_route = respx.patch(f"{BASE}/zones/zone-1/dns_records/cf-1")

    client = CloudflareClient(api_token="test-token")
    result = await SwitchService(session, client).execute_single_switch(
        target.id, dc2.id, actor="tester"
    )
    await client.aclose()

    assert result.skipped is True
    assert result.success is True
    assert not update_route.called
    assert target.current_datacenter_id == dc2.id
    assert result.audit_log_entry.status == AuditStatus.success


@respx.mock
async def test_execute_single_switch_logs_failure_without_raising(session):
    domain = await _make_domain(session)
    dc1 = await _make_datacenter(session, "dc1", "1.1.1.1")
    dc2 = await _make_datacenter(session, "dc2", "2.2.2.2")
    target = await _make_target(session, domain, dc1)

    respx.get(f"{BASE}/zones/zone-1/dns_records/cf-1").mock(
        return_value=_cf_ok({"id": "cf-1", "content": "1.1.1.1"})
    )
    respx.patch(f"{BASE}/zones/zone-1/dns_records/cf-1").mock(
        return_value=_cf_error(400, "invalid record")
    )

    client = CloudflareClient(api_token="test-token")
    result = await SwitchService(session, client).execute_single_switch(
        target.id, dc2.id, actor="tester"
    )
    await client.aclose()

    assert result.success is False
    assert result.error_message is not None
    assert target.current_datacenter_id == dc1.id  # unchanged
    assert result.audit_log_entry.status == AuditStatus.failed
    assert result.audit_log_entry.error_message is not None


# --- full plan -> execute -> rollback flow ----------------------------------


@respx.mock
async def test_full_switch_then_rollback_flow(session):
    domain = await _make_domain(session)
    dc1 = await _make_datacenter(session, "dc1", "1.1.1.1")
    dc2 = await _make_datacenter(session, "dc2", "2.2.2.2")
    target = await _make_target(session, domain, dc1)

    respx.get(f"{BASE}/zones/zone-1/dns_records/cf-1").mock(
        side_effect=[
            _cf_ok({"id": "cf-1", "content": "1.1.1.1"}),  # pre-switch live check
            _cf_ok({"id": "cf-1", "content": "2.2.2.2"}),  # pre-rollback live check
        ]
    )
    respx.patch(f"{BASE}/zones/zone-1/dns_records/cf-1").mock(
        side_effect=[
            _cf_ok({"id": "cf-1", "content": "2.2.2.2"}),  # switch write
            _cf_ok({"id": "cf-1", "content": "1.1.1.1"}),  # rollback write
        ]
    )

    client = CloudflareClient(api_token="test-token")
    service = SwitchService(session, client)

    plan = await service.plan_single_switch(target.id, dc2.id)
    assert plan.no_op is False

    switch_result = await service.execute_single_switch(target.id, dc2.id, actor="alice")
    assert switch_result.success is True
    assert target.current_datacenter_id == dc2.id
    original_entry_id = switch_result.audit_log_entry.id

    rollback_result = await service.rollback(original_entry_id, actor="bob")
    await client.aclose()

    assert rollback_result.success is True
    assert target.current_datacenter_id == dc1.id
    assert rollback_result.audit_log_entry.rollback_of_id == original_entry_id
    assert rollback_result.audit_log_entry.actor == "bob"

    await session.refresh(switch_result.audit_log_entry)
    assert switch_result.audit_log_entry.status == AuditStatus.rolled_back


async def test_rollback_raises_if_original_not_successful(session):
    domain = await _make_domain(session)
    dc1 = await _make_datacenter(session, "dc1", "1.1.1.1")
    dc2 = await _make_datacenter(session, "dc2", "2.2.2.2")
    target = await _make_target(session, domain, dc1)

    client = CloudflareClient(api_token="test-token")
    service = SwitchService(session, client)

    from app.models import ActionType, AuditLogEntry

    failed_entry = AuditLogEntry(
        actor="tester",
        action_type=ActionType.single,
        dns_target_id=target.id,
        previous_datacenter_id=dc1.id,
        new_datacenter_id=dc2.id,
        cloudflare_record_id="cf-1",
        status=AuditStatus.failed,
        error_message="boom",
    )
    session.add(failed_entry)
    await session.flush()

    with pytest.raises(SwitchValidationError, match="nothing to roll back"):
        await service.rollback(failed_entry.id, actor="bob")
    await client.aclose()


# --- bulk switch: partial failure tolerance ---------------------------------


@respx.mock
async def test_execute_bulk_switch_continues_after_one_failure(session):
    domain = await _make_domain(session)
    dc1 = await _make_datacenter(session, "dc1", "1.1.1.1")
    dc2 = await _make_datacenter(session, "dc2", "2.2.2.2")
    target_a = await _make_target(session, domain, dc1, name="a", cf_record_id="cf-a")
    target_b = await _make_target(session, domain, dc1, name="b", cf_record_id="cf-b")

    group = SwitchGroup(name="group1")
    session.add(group)
    await session.flush()
    session.add_all(
        [
            SwitchGroupMember(switch_group_id=group.id, dns_target_id=target_a.id, position=0),
            SwitchGroupMember(switch_group_id=group.id, dns_target_id=target_b.id, position=1),
        ]
    )
    await session.flush()

    respx.get(f"{BASE}/zones/zone-1/dns_records/cf-a").mock(
        return_value=_cf_ok({"id": "cf-a", "content": "1.1.1.1"})
    )
    respx.patch(f"{BASE}/zones/zone-1/dns_records/cf-a").mock(
        return_value=_cf_error(400, "cloudflare rejected this record")
    )
    respx.get(f"{BASE}/zones/zone-1/dns_records/cf-b").mock(
        return_value=_cf_ok({"id": "cf-b", "content": "1.1.1.1"})
    )
    respx.patch(f"{BASE}/zones/zone-1/dns_records/cf-b").mock(
        return_value=_cf_ok({"id": "cf-b", "content": "2.2.2.2"})
    )

    client = CloudflareClient(api_token="test-token")
    summary = await SwitchService(session, client).execute_bulk_switch(
        group.id, dc2.id, actor="tester"
    )
    await client.aclose()

    assert len(summary.succeeded) == 1
    assert len(summary.failed) == 1
    assert summary.failed[0].dns_target_id == target_a.id
    assert summary.succeeded[0].dns_target_id == target_b.id
    assert target_a.current_datacenter_id == dc1.id  # unchanged after failure
    assert target_b.current_datacenter_id == dc2.id
