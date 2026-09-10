import httpx
import respx

from app.cloudflare.client import CloudflareClient
from app.models import Datacenter, DnsTarget, Domain, RecordType
from app.services.sync_service import SyncService

BASE = "https://api.cloudflare.com/client/v4"


def _cf_response(records):
    return httpx.Response(200, json={"success": True, "result": records})


async def _make_domain(session, name="example.com", zone="zone-1"):
    domain = Domain(name=name, cloudflare_zone_id=zone)
    session.add(domain)
    await session.flush()
    return domain


@respx.mock
async def test_fills_missing_cloudflare_record_id(session):
    domain = await _make_domain(session)
    target = DnsTarget(domain_id=domain.id, name="www", record_type=RecordType.A)
    session.add(target)
    await session.flush()

    respx.get(f"{BASE}/zones/zone-1/dns_records").mock(
        return_value=_cf_response(
            [{"id": "cf-rec-1", "name": "www.example.com", "type": "A", "content": "1.2.3.4"}]
        )
    )

    async with CloudflareClient(api_token="test-token") as client:
        result = await SyncService(session, client).sync_domain(domain)

    assert result.matched_target_ids == [target.id]
    assert result.filled_record_id_target_ids == [target.id]
    assert target.cloudflare_record_id == "cf-rec-1"


@respx.mock
async def test_root_domain_matches_at_sign(session):
    domain = await _make_domain(session)
    target = DnsTarget(domain_id=domain.id, name="@", record_type=RecordType.A)
    session.add(target)
    await session.flush()

    respx.get(f"{BASE}/zones/zone-1/dns_records").mock(
        return_value=_cf_response(
            [{"id": "cf-rec-root", "name": "example.com", "type": "A", "content": "1.2.3.4"}]
        )
    )

    async with CloudflareClient(api_token="test-token") as client:
        result = await SyncService(session, client).sync_domain(domain)

    assert result.matched_target_ids == [target.id]
    assert target.cloudflare_record_id == "cf-rec-root"


@respx.mock
async def test_unmatched_target_when_cloudflare_has_no_record(session):
    domain = await _make_domain(session)
    target = DnsTarget(domain_id=domain.id, name="missing", record_type=RecordType.A)
    session.add(target)
    await session.flush()

    respx.get(f"{BASE}/zones/zone-1/dns_records").mock(return_value=_cf_response([]))

    async with CloudflareClient(api_token="test-token") as client:
        result = await SyncService(session, client).sync_domain(domain)

    assert result.unmatched_target_ids == [target.id]
    assert result.matched_target_ids == []


@respx.mock
async def test_flags_datacenter_mismatch_without_auto_fixing(session):
    domain = await _make_domain(session)
    dc_primary = Datacenter(name="dc-primary", ip_address="1.2.3.4")
    dc_secondary = Datacenter(name="dc-secondary", ip_address="5.6.7.8")
    session.add_all([dc_primary, dc_secondary])
    await session.flush()

    # DB thinks this target points at dc-primary, but Cloudflare's actual
    # content resolves to dc-secondary's IP -- e.g. changed by hand.
    target = DnsTarget(
        domain_id=domain.id,
        name="www",
        record_type=RecordType.A,
        current_datacenter_id=dc_primary.id,
    )
    session.add(target)
    await session.flush()

    respx.get(f"{BASE}/zones/zone-1/dns_records").mock(
        return_value=_cf_response(
            [{"id": "cf-rec-1", "name": "www.example.com", "type": "A", "content": "5.6.7.8"}]
        )
    )

    async with CloudflareClient(api_token="test-token") as client:
        result = await SyncService(session, client).sync_domain(domain)

    assert len(result.datacenter_mismatches) == 1
    mismatch = result.datacenter_mismatches[0]
    assert mismatch.dns_target_id == target.id
    assert mismatch.expected_datacenter_id == dc_primary.id
    assert mismatch.resolved_datacenter_id == dc_secondary.id
    # not auto-fixed
    assert target.current_datacenter_id == dc_primary.id


@respx.mock
async def test_no_mismatch_when_content_matches_current_datacenter(session):
    domain = await _make_domain(session)
    dc = Datacenter(name="dc-primary", ip_address="1.2.3.4")
    session.add(dc)
    await session.flush()

    target = DnsTarget(
        domain_id=domain.id,
        name="www",
        record_type=RecordType.A,
        current_datacenter_id=dc.id,
    )
    session.add(target)
    await session.flush()

    respx.get(f"{BASE}/zones/zone-1/dns_records").mock(
        return_value=_cf_response(
            [{"id": "cf-rec-1", "name": "www.example.com", "type": "A", "content": "1.2.3.4"}]
        )
    )

    async with CloudflareClient(api_token="test-token") as client:
        result = await SyncService(session, client).sync_domain(domain)

    assert result.datacenter_mismatches == []


@respx.mock
async def test_no_mismatch_when_content_unknown_and_no_datacenter_assigned(session):
    domain = await _make_domain(session)
    target = DnsTarget(domain_id=domain.id, name="www", record_type=RecordType.A)
    session.add(target)
    await session.flush()

    respx.get(f"{BASE}/zones/zone-1/dns_records").mock(
        return_value=_cf_response(
            [{"id": "cf-rec-1", "name": "www.example.com", "type": "A", "content": "9.9.9.9"}]
        )
    )

    async with CloudflareClient(api_token="test-token") as client:
        result = await SyncService(session, client).sync_domain(domain)

    assert result.datacenter_mismatches == []
