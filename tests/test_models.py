import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import selectinload

from app.models import (
    ActionType,
    AuditLogEntry,
    AuditStatus,
    Datacenter,
    DatacenterStatus,
    DnsTarget,
    Domain,
    RecordType,
    SwitchGroup,
    SwitchGroupMember,
)


async def _make_domain(session, name="example.com", zone="zone-1"):
    domain = Domain(name=name, cloudflare_zone_id=zone)
    session.add(domain)
    await session.flush()
    return domain


async def _make_datacenter(session, name="dc1"):
    dc = Datacenter(name=name)
    session.add(dc)
    await session.flush()
    return dc


async def test_datacenter_name_unique(session):
    session.add_all([Datacenter(name="dc1"), Datacenter(name="dc1")])
    with pytest.raises(IntegrityError):
        await session.commit()


async def test_datacenter_default_status(session):
    dc = Datacenter(name="dc1")
    session.add(dc)
    await session.commit()
    await session.refresh(dc)
    assert dc.status == DatacenterStatus.active


async def test_dns_target_unique_domain_name_type(session):
    domain = await _make_domain(session)
    session.add_all(
        [
            DnsTarget(domain_id=domain.id, name="www", record_type=RecordType.A),
            DnsTarget(domain_id=domain.id, name="www", record_type=RecordType.A),
        ]
    )
    with pytest.raises(IntegrityError):
        await session.commit()


async def test_restrict_delete_datacenter_referenced_by_dns_target(session):
    domain = await _make_domain(session)
    dc = await _make_datacenter(session)
    session.add(DnsTarget(domain_id=domain.id, name="www", record_type=RecordType.A, current_datacenter_id=dc.id))
    await session.commit()

    with pytest.raises(IntegrityError):
        await session.execute(text("DELETE FROM datacenters WHERE id = :id"), {"id": dc.id})
        await session.commit()
    await session.rollback()


async def test_restrict_delete_datacenter_referenced_by_audit_log(session):
    dc1 = await _make_datacenter(session, "dc1")
    dc2 = await _make_datacenter(session, "dc2")
    session.add(
        AuditLogEntry(
            actor="tester",
            action_type=ActionType.single,
            previous_datacenter_id=dc1.id,
            new_datacenter_id=dc2.id,
            cloudflare_record_id="rec-1",
            status=AuditStatus.success,
        )
    )
    await session.commit()

    with pytest.raises(IntegrityError):
        await session.execute(text("DELETE FROM datacenters WHERE id = :id"), {"id": dc1.id})
        await session.commit()
    await session.rollback()


async def test_restrict_delete_domain_referenced_by_dns_target(session):
    domain = await _make_domain(session)
    session.add(DnsTarget(domain_id=domain.id, name="www", record_type=RecordType.A))
    await session.commit()

    with pytest.raises(IntegrityError):
        await session.execute(text("DELETE FROM domains WHERE id = :id"), {"id": domain.id})
        await session.commit()
    await session.rollback()


async def test_cascade_delete_switch_group_removes_members(session):
    domain = await _make_domain(session)
    target = DnsTarget(domain_id=domain.id, name="www", record_type=RecordType.A)
    group = SwitchGroup(name="group1")
    session.add_all([target, group])
    await session.flush()
    session.add(SwitchGroupMember(switch_group_id=group.id, dns_target_id=target.id, position=0))
    await session.commit()

    await session.execute(text("DELETE FROM switch_groups WHERE id = :id"), {"id": group.id})
    await session.commit()

    result = await session.execute(select(SwitchGroupMember))
    assert result.scalars().all() == []


async def test_cascade_delete_dns_target_removes_group_membership(session):
    domain = await _make_domain(session)
    target = DnsTarget(domain_id=domain.id, name="www", record_type=RecordType.A)
    group = SwitchGroup(name="group1")
    session.add_all([target, group])
    await session.flush()
    session.add(SwitchGroupMember(switch_group_id=group.id, dns_target_id=target.id, position=0))
    await session.commit()

    await session.execute(text("DELETE FROM dns_targets WHERE id = :id"), {"id": target.id})
    await session.commit()

    result = await session.execute(select(SwitchGroupMember))
    assert result.scalars().all() == []


async def test_delete_dns_target_sets_null_on_audit_log(session):
    domain = await _make_domain(session)
    dc1 = await _make_datacenter(session, "dc1")
    dc2 = await _make_datacenter(session, "dc2")
    target = DnsTarget(domain_id=domain.id, name="www", record_type=RecordType.A)
    session.add(target)
    await session.flush()
    entry = AuditLogEntry(
        actor="tester",
        action_type=ActionType.single,
        dns_target_id=target.id,
        previous_datacenter_id=dc1.id,
        new_datacenter_id=dc2.id,
        cloudflare_record_id="rec-1",
        status=AuditStatus.success,
    )
    session.add(entry)
    await session.commit()

    await session.execute(text("DELETE FROM dns_targets WHERE id = :id"), {"id": target.id})
    await session.commit()

    await session.refresh(entry)
    assert entry.dns_target_id is None


async def test_delete_switch_group_sets_null_on_audit_log(session):
    domain = await _make_domain(session)
    dc1 = await _make_datacenter(session, "dc1")
    dc2 = await _make_datacenter(session, "dc2")
    group = SwitchGroup(name="group1")
    session.add(group)
    await session.flush()
    entry = AuditLogEntry(
        actor="tester",
        action_type=ActionType.bulk,
        switch_group_id=group.id,
        previous_datacenter_id=dc1.id,
        new_datacenter_id=dc2.id,
        cloudflare_record_id="rec-1",
        status=AuditStatus.success,
    )
    session.add(entry)
    await session.commit()

    await session.execute(text("DELETE FROM switch_groups WHERE id = :id"), {"id": group.id})
    await session.commit()

    await session.refresh(entry)
    assert entry.switch_group_id is None


async def test_switch_group_member_ordering(session):
    domain = await _make_domain(session)
    t1 = DnsTarget(domain_id=domain.id, name="a", record_type=RecordType.A)
    t2 = DnsTarget(domain_id=domain.id, name="b", record_type=RecordType.A)
    group = SwitchGroup(name="group1")
    session.add_all([t1, t2, group])
    await session.flush()
    session.add_all(
        [
            SwitchGroupMember(switch_group_id=group.id, dns_target_id=t2.id, position=1),
            SwitchGroupMember(switch_group_id=group.id, dns_target_id=t1.id, position=0),
        ]
    )
    await session.commit()

    result = await session.execute(
        select(SwitchGroup)
        .where(SwitchGroup.id == group.id)
        .options(selectinload(SwitchGroup.members))
    )
    loaded = result.scalar_one()
    assert [m.dns_target_id for m in loaded.members] == [t1.id, t2.id]
