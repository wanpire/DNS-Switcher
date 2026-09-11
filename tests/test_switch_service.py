import httpx
import pytest
import respx

from app.cloudflare.client import CloudflareClient
from app.models import (
    AuditStatus,
    Datacenter,
    DnsTarget,
    Domain,
    RecordType,
    SwitchGroup,
    SwitchGroupMember,
    TargetDatacenterIp,
)
from app.services.switch_service import SlotDiff, SwitchService, SwitchValidationError

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


async def _add_ips(session, target, datacenter, ips_with_records):
    """ips_with_records: list of (ip, cloudflare_record_id | None)"""
    for slot_index, (ip, record_id) in enumerate(ips_with_records):
        session.add(
            TargetDatacenterIp(
                dns_target_id=target.id,
                datacenter_id=datacenter.id,
                slot_index=slot_index,
                ip_address=ip,
                cloudflare_record_id=record_id,
            )
        )
    await session.flush()


# --- plan_single_switch: pure DB diff, never calls Cloudflare -------------


async def test_plan_single_switch_never_calls_cloudflare(session):
    domain = await _make_domain(session)
    dc1 = await _make_datacenter(session, "dc1")
    dc2 = await _make_datacenter(session, "dc2")
    target = await _make_target(session, domain)
    await _add_ips(session, target, dc1, [("1.1.1.1", "cf-1")])
    await _add_ips(session, target, dc2, [("2.2.2.2", None)])
    target.current_datacenter_id = dc1.id
    await session.flush()

    client = CloudflareClient(api_token="test-token")
    plan = await SwitchService(session, client).plan_single_switch(target.id, dc2.id)
    await client.aclose()

    assert plan.slot_diffs == [
        SlotDiff(slot_index=0, current_ip="1.1.1.1", new_ip="2.2.2.2", cloudflare_record_id="cf-1")
    ]
    assert plan.no_op is False
    assert plan.slot_count_mismatch is False


async def test_plan_single_switch_no_op_when_already_on_target(session):
    domain = await _make_domain(session)
    dc1 = await _make_datacenter(session, "dc1")
    target = await _make_target(session, domain)
    await _add_ips(session, target, dc1, [("1.1.1.1", "cf-1")])
    target.current_datacenter_id = dc1.id
    await session.flush()

    client = CloudflareClient(api_token="test-token")
    plan = await SwitchService(session, client).plan_single_switch(target.id, dc1.id)
    await client.aclose()
    assert plan.no_op is True


async def test_plan_single_switch_raises_when_destination_has_no_ips(session):
    domain = await _make_domain(session)
    dc1 = await _make_datacenter(session, "dc1")
    dc2 = await _make_datacenter(session, "dc2")
    target = await _make_target(session, domain)
    await _add_ips(session, target, dc1, [("1.1.1.1", "cf-1")])

    client = CloudflareClient(api_token="test-token")
    with pytest.raises(SwitchValidationError, match="No IPs configured"):
        await SwitchService(session, client).plan_single_switch(target.id, dc2.id)
    await client.aclose()


async def test_plan_single_switch_detects_slot_count_mismatch(session):
    domain = await _make_domain(session)
    dc1 = await _make_datacenter(session, "dc1")
    dc2 = await _make_datacenter(session, "dc2")
    target = await _make_target(session, domain)
    await _add_ips(session, target, dc1, [("1.1.1.1", "cf-1")])
    await _add_ips(session, target, dc2, [("2.2.2.1", None), ("2.2.2.2", None)])
    target.current_datacenter_id = dc1.id
    await session.flush()

    client = CloudflareClient(api_token="test-token")
    plan = await SwitchService(session, client).plan_single_switch(target.id, dc2.id)
    await client.aclose()

    assert plan.slot_count_mismatch is True
    assert len(plan.slot_diffs) == 2
    assert plan.slot_diffs[0].current_ip == "1.1.1.1"
    assert plan.slot_diffs[1].current_ip is None  # source has no slot 1


# --- execute_single_switch ---------------------------------------------


@respx.mock
async def test_execute_single_switch_single_slot_success(session):
    domain = await _make_domain(session)
    dc1 = await _make_datacenter(session, "dc1")
    dc2 = await _make_datacenter(session, "dc2")
    target = await _make_target(session, domain)
    await _add_ips(session, target, dc1, [("1.1.1.1", "cf-1")])
    await _add_ips(session, target, dc2, [("2.2.2.2", None)])
    target.current_datacenter_id = dc1.id
    await session.flush()

    respx.get(f"{BASE}/zones/zone-1/dns_records/cf-1").mock(
        return_value=_cf_ok({"id": "cf-1", "content": "1.1.1.1"})
    )
    update_route = respx.patch(f"{BASE}/zones/zone-1/dns_records/cf-1").mock(
        return_value=_cf_ok({"id": "cf-1", "content": "2.2.2.2"})
    )

    client = CloudflareClient(api_token="test-token")
    result = await SwitchService(session, client).execute_single_switch(target.id, dc2.id, actor="tester")
    await client.aclose()

    assert result.success is True
    assert update_route.called
    assert target.current_datacenter_id == dc2.id

    await session.refresh(target, attribute_names=["datacenter_ips"])
    rows = {(r.datacenter_id, r.slot_index): r for r in target.datacenter_ips}
    assert rows[(dc2.id, 0)].cloudflare_record_id == "cf-1"
    assert rows[(dc1.id, 0)].cloudflare_record_id is None


@respx.mock
async def test_execute_single_switch_skips_write_when_already_correct(session):
    domain = await _make_domain(session)
    dc1 = await _make_datacenter(session, "dc1")
    dc2 = await _make_datacenter(session, "dc2")
    target = await _make_target(session, domain)
    await _add_ips(session, target, dc1, [("1.1.1.1", "cf-1")])
    await _add_ips(session, target, dc2, [("2.2.2.2", None)])
    target.current_datacenter_id = dc1.id
    await session.flush()

    respx.get(f"{BASE}/zones/zone-1/dns_records/cf-1").mock(
        return_value=_cf_ok({"id": "cf-1", "content": "2.2.2.2"})
    )
    update_route = respx.patch(f"{BASE}/zones/zone-1/dns_records/cf-1")

    client = CloudflareClient(api_token="test-token")
    result = await SwitchService(session, client).execute_single_switch(target.id, dc2.id, actor="tester")
    await client.aclose()

    assert result.skipped is True
    assert result.success is True
    assert not update_route.called
    assert target.current_datacenter_id == dc2.id


@respx.mock
async def test_execute_single_switch_logs_failure_without_raising(session):
    domain = await _make_domain(session)
    dc1 = await _make_datacenter(session, "dc1")
    dc2 = await _make_datacenter(session, "dc2")
    target = await _make_target(session, domain)
    await _add_ips(session, target, dc1, [("1.1.1.1", "cf-1")])
    await _add_ips(session, target, dc2, [("2.2.2.2", None)])
    target.current_datacenter_id = dc1.id
    await session.flush()

    respx.get(f"{BASE}/zones/zone-1/dns_records/cf-1").mock(
        return_value=_cf_ok({"id": "cf-1", "content": "1.1.1.1"})
    )
    respx.patch(f"{BASE}/zones/zone-1/dns_records/cf-1").mock(return_value=_cf_error(400))

    client = CloudflareClient(api_token="test-token")
    result = await SwitchService(session, client).execute_single_switch(target.id, dc2.id, actor="tester")
    await client.aclose()

    assert result.success is False
    assert target.current_datacenter_id == dc1.id  # unchanged
    assert result.audit_log_entry.status == AuditStatus.failed


@respx.mock
async def test_execute_single_switch_multi_slot_reuses_records(session):
    domain = await _make_domain(session)
    dc1 = await _make_datacenter(session, "dc1")
    dc2 = await _make_datacenter(session, "dc2")
    target = await _make_target(session, domain)
    await _add_ips(session, target, dc1, [("1.1.1.1", "cf-1"), ("1.1.1.2", "cf-2")])
    await _add_ips(session, target, dc2, [("2.2.2.1", None), ("2.2.2.2", None)])
    target.current_datacenter_id = dc1.id
    await session.flush()

    respx.get(f"{BASE}/zones/zone-1/dns_records/cf-1").mock(
        return_value=_cf_ok({"id": "cf-1", "content": "1.1.1.1"})
    )
    respx.get(f"{BASE}/zones/zone-1/dns_records/cf-2").mock(
        return_value=_cf_ok({"id": "cf-2", "content": "1.1.1.2"})
    )
    respx.patch(f"{BASE}/zones/zone-1/dns_records/cf-1").mock(
        return_value=_cf_ok({"id": "cf-1", "content": "2.2.2.1"})
    )
    respx.patch(f"{BASE}/zones/zone-1/dns_records/cf-2").mock(
        return_value=_cf_ok({"id": "cf-2", "content": "2.2.2.2"})
    )

    client = CloudflareClient(api_token="test-token")
    result = await SwitchService(session, client).execute_single_switch(target.id, dc2.id, actor="tester")
    await client.aclose()

    assert result.success is True
    assert len(result.slot_results) == 2
    assert all(r["action"] == "updated" for r in result.slot_results)
    await session.refresh(target, attribute_names=["datacenter_ips"])
    rows = {(r.datacenter_id, r.slot_index): r for r in target.datacenter_ips}
    assert rows[(dc2.id, 0)].cloudflare_record_id == "cf-1"
    assert rows[(dc2.id, 1)].cloudflare_record_id == "cf-2"
    assert rows[(dc1.id, 0)].cloudflare_record_id is None
    assert rows[(dc1.id, 1)].cloudflare_record_id is None


async def test_execute_single_switch_refuses_slot_count_mismatch_by_default(session):
    domain = await _make_domain(session)
    dc1 = await _make_datacenter(session, "dc1")
    dc2 = await _make_datacenter(session, "dc2")
    target = await _make_target(session, domain)
    await _add_ips(session, target, dc1, [("1.1.1.1", "cf-1")])
    await _add_ips(session, target, dc2, [("2.2.2.1", None), ("2.2.2.2", None)])
    target.current_datacenter_id = dc1.id
    await session.flush()

    client = CloudflareClient(api_token="test-token")
    with pytest.raises(SwitchValidationError, match="allow_slot_count_mismatch"):
        await SwitchService(session, client).execute_single_switch(target.id, dc2.id, actor="tester")
    await client.aclose()


@respx.mock
async def test_execute_single_switch_allows_slot_count_mismatch_when_confirmed(session):
    """destination has 2 slots, source has 1 -- confirmed via
    allow_slot_count_mismatch, so slot 0 is PATCHed (reusing cf-1) and slot
    1 is a brand new record (create)."""
    domain = await _make_domain(session)
    dc1 = await _make_datacenter(session, "dc1")
    dc2 = await _make_datacenter(session, "dc2")
    target = await _make_target(session, domain)
    await _add_ips(session, target, dc1, [("1.1.1.1", "cf-1")])
    await _add_ips(session, target, dc2, [("2.2.2.1", None), ("2.2.2.2", None)])
    target.current_datacenter_id = dc1.id
    await session.flush()

    respx.get(f"{BASE}/zones/zone-1/dns_records/cf-1").mock(
        return_value=_cf_ok({"id": "cf-1", "content": "1.1.1.1"})
    )
    respx.patch(f"{BASE}/zones/zone-1/dns_records/cf-1").mock(
        return_value=_cf_ok({"id": "cf-1", "content": "2.2.2.1"})
    )
    create_route = respx.post(f"{BASE}/zones/zone-1/dns_records").mock(
        return_value=_cf_ok({"id": "cf-new", "content": "2.2.2.2"})
    )

    client = CloudflareClient(api_token="test-token")
    result = await SwitchService(session, client).execute_single_switch(
        target.id, dc2.id, actor="tester", allow_slot_count_mismatch=True
    )
    await client.aclose()

    assert result.success is True
    assert create_route.called
    actions = {r["slot_index"]: r["action"] for r in result.slot_results}
    assert actions == {0: "updated", 1: "created"}
    await session.refresh(target, attribute_names=["datacenter_ips"])
    rows = {(r.datacenter_id, r.slot_index): r for r in target.datacenter_ips}
    assert rows[(dc2.id, 1)].cloudflare_record_id == "cf-new"


@respx.mock
async def test_execute_single_switch_deletes_extra_record_when_shrinking(session):
    """source has 2 slots, destination has 1 -- confirmed via
    allow_slot_count_mismatch, slot 1's record gets deleted."""
    domain = await _make_domain(session)
    dc1 = await _make_datacenter(session, "dc1")
    dc2 = await _make_datacenter(session, "dc2")
    target = await _make_target(session, domain)
    await _add_ips(session, target, dc1, [("1.1.1.1", "cf-1"), ("1.1.1.2", "cf-2")])
    await _add_ips(session, target, dc2, [("2.2.2.1", None)])
    target.current_datacenter_id = dc1.id
    await session.flush()

    respx.get(f"{BASE}/zones/zone-1/dns_records/cf-1").mock(
        return_value=_cf_ok({"id": "cf-1", "content": "1.1.1.1"})
    )
    respx.patch(f"{BASE}/zones/zone-1/dns_records/cf-1").mock(
        return_value=_cf_ok({"id": "cf-1", "content": "2.2.2.1"})
    )
    delete_route = respx.delete(f"{BASE}/zones/zone-1/dns_records/cf-2").mock(
        return_value=_cf_ok({"id": "cf-2"})
    )

    client = CloudflareClient(api_token="test-token")
    result = await SwitchService(session, client).execute_single_switch(
        target.id, dc2.id, actor="tester", allow_slot_count_mismatch=True
    )
    await client.aclose()

    assert result.success is True
    assert delete_route.called
    actions = {r["slot_index"]: r["action"] for r in result.slot_results}
    assert actions == {0: "updated", 1: "deleted"}
    await session.refresh(target, attribute_names=["datacenter_ips"])
    rows = {(r.datacenter_id, r.slot_index): r for r in target.datacenter_ips}
    assert rows[(dc1.id, 1)].cloudflare_record_id is None


# --- full plan -> execute -> rollback flow ----------------------------------


@respx.mock
async def test_full_switch_then_rollback_flow(session):
    domain = await _make_domain(session)
    dc1 = await _make_datacenter(session, "dc1")
    dc2 = await _make_datacenter(session, "dc2")
    target = await _make_target(session, domain)
    await _add_ips(session, target, dc1, [("1.1.1.1", "cf-1")])
    await _add_ips(session, target, dc2, [("2.2.2.2", None)])
    target.current_datacenter_id = dc1.id
    await session.flush()

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

    await session.refresh(switch_result.audit_log_entry)
    assert switch_result.audit_log_entry.status == AuditStatus.rolled_back


async def test_rollback_raises_if_original_not_successful(session):
    domain = await _make_domain(session)
    dc1 = await _make_datacenter(session, "dc1")
    dc2 = await _make_datacenter(session, "dc2")
    target = await _make_target(session, domain)
    await _add_ips(session, target, dc1, [("1.1.1.1", "cf-1")])

    from app.models import ActionType, AuditLogEntry

    failed_entry = AuditLogEntry(
        actor="tester",
        action_type=ActionType.single,
        dns_target_id=target.id,
        previous_datacenter_id=dc1.id,
        new_datacenter_id=dc2.id,
        status=AuditStatus.failed,
        error_message="boom",
    )
    session.add(failed_entry)
    await session.flush()

    client = CloudflareClient(api_token="test-token")
    with pytest.raises(SwitchValidationError, match="nothing to roll back"):
        await SwitchService(session, client).rollback(failed_entry.id, actor="bob")
    await client.aclose()


# --- bulk switch: partial failure tolerance ---------------------------------


@respx.mock
async def test_execute_bulk_switch_continues_after_one_failure(session, monkeypatch):
    from types import SimpleNamespace

    monkeypatch.setattr(
        "app.services.switch_service.get_settings",
        lambda: SimpleNamespace(bulk_switch_delay_seconds=0),
    )

    domain = await _make_domain(session)
    dc1 = await _make_datacenter(session, "dc1")
    dc2 = await _make_datacenter(session, "dc2")

    target_a = await _make_target(session, domain, name="a")
    await _add_ips(session, target_a, dc1, [("1.1.1.1", "cf-a")])
    await _add_ips(session, target_a, dc2, [("2.2.2.1", None)])
    target_a.current_datacenter_id = dc1.id

    target_b = await _make_target(session, domain, name="b")
    await _add_ips(session, target_b, dc1, [("1.1.1.2", "cf-b")])
    await _add_ips(session, target_b, dc2, [("2.2.2.2", None)])
    target_b.current_datacenter_id = dc1.id
    await session.flush()

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
    respx.patch(f"{BASE}/zones/zone-1/dns_records/cf-a").mock(return_value=_cf_error(400))
    respx.get(f"{BASE}/zones/zone-1/dns_records/cf-b").mock(
        return_value=_cf_ok({"id": "cf-b", "content": "1.1.1.2"})
    )
    respx.patch(f"{BASE}/zones/zone-1/dns_records/cf-b").mock(
        return_value=_cf_ok({"id": "cf-b", "content": "2.2.2.2"})
    )

    client = CloudflareClient(api_token="test-token")
    summary = await SwitchService(session, client).execute_bulk_switch(group.id, dc2.id, actor="tester")
    await client.aclose()

    assert len(summary.succeeded) == 1
    assert len(summary.failed) == 1
    assert summary.failed[0].dns_target_id == target_a.id
    assert summary.succeeded[0].dns_target_id == target_b.id
    assert target_a.current_datacenter_id == dc1.id
    assert target_b.current_datacenter_id == dc2.id


@respx.mock
async def test_bulk_switch_member_with_slot_mismatch_fails_cleanly(session, monkeypatch):
    """A bulk switch never silently allows a slot-count mismatch -- that
    member fails and the rest continue."""
    from types import SimpleNamespace

    monkeypatch.setattr(
        "app.services.switch_service.get_settings",
        lambda: SimpleNamespace(bulk_switch_delay_seconds=0),
    )

    domain = await _make_domain(session)
    dc1 = await _make_datacenter(session, "dc1")
    dc2 = await _make_datacenter(session, "dc2")

    mismatched = await _make_target(session, domain, name="mismatched")
    await _add_ips(session, mismatched, dc1, [("1.1.1.1", "cf-1")])
    await _add_ips(session, mismatched, dc2, [("2.2.2.1", None), ("2.2.2.2", None)])
    mismatched.current_datacenter_id = dc1.id

    clean = await _make_target(session, domain, name="clean")
    await _add_ips(session, clean, dc1, [("1.1.1.9", "cf-9")])
    await _add_ips(session, clean, dc2, [("2.2.2.9", None)])
    clean.current_datacenter_id = dc1.id
    await session.flush()

    group = SwitchGroup(name="group1")
    session.add(group)
    await session.flush()
    session.add_all(
        [
            SwitchGroupMember(switch_group_id=group.id, dns_target_id=mismatched.id, position=0),
            SwitchGroupMember(switch_group_id=group.id, dns_target_id=clean.id, position=1),
        ]
    )
    await session.flush()

    respx.get(f"{BASE}/zones/zone-1/dns_records/cf-9").mock(
        return_value=_cf_ok({"id": "cf-9", "content": "1.1.1.9"})
    )
    respx.patch(f"{BASE}/zones/zone-1/dns_records/cf-9").mock(
        return_value=_cf_ok({"id": "cf-9", "content": "2.2.2.9"})
    )

    client = CloudflareClient(api_token="test-token")
    summary = await SwitchService(session, client).execute_bulk_switch(group.id, dc2.id, actor="tester")
    await client.aclose()

    assert len(summary.failed) == 1
    assert summary.failed[0].dns_target_id == mismatched.id
    assert "allow_slot_count_mismatch" in summary.failed[0].error_message
    assert len(summary.succeeded) == 1
    assert summary.succeeded[0].dns_target_id == clean.id
