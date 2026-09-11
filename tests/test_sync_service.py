import httpx
import respx

from app.cloudflare.client import CloudflareClient
from app.models import Datacenter, DnsTarget, Domain, RecordType, TargetDatacenterIp
from app.services.sync_service import SyncService

BASE = "https://api.cloudflare.com/client/v4"


def _cf_response(records):
    return httpx.Response(200, json={"success": True, "result": records})


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


async def _make_target_with_ips(session, domain, *, name="www", candidates: dict):
    """candidates: {datacenter: [ip, ip, ...]}"""
    target = DnsTarget(domain_id=domain.id, name=name, record_type=RecordType.A)
    session.add(target)
    await session.flush()
    for dc, ips in candidates.items():
        for slot_index, ip in enumerate(ips):
            session.add(
                TargetDatacenterIp(
                    dns_target_id=target.id, datacenter_id=dc.id, slot_index=slot_index, ip_address=ip
                )
            )
    await session.flush()
    return target


@respx.mock
async def test_fills_missing_cloudflare_record_id(session):
    domain = await _make_domain(session)
    dc1 = await _make_datacenter(session, "dc1")
    dc2 = await _make_datacenter(session, "dc2")
    target = await _make_target_with_ips(
        session, domain, candidates={dc1: ["1.1.1.1"], dc2: ["2.2.2.2"]}
    )

    respx.get(f"{BASE}/zones/zone-1/dns_records").mock(
        return_value=_cf_response(
            [{"id": "cf-rec-1", "name": "www.example.com", "type": "A", "content": "1.1.1.1"}]
        )
    )

    async with CloudflareClient(api_token="test-token") as client:
        result = await SyncService(session, client).sync_domain(domain)

    assert result.matched_target_ids == [target.id]
    assert result.filled_record_id_count == 1
    await session.refresh(target, attribute_names=["datacenter_ips"])
    filled = next(row for row in target.datacenter_ips if row.datacenter_id == dc1.id)
    assert filled.cloudflare_record_id == "cf-rec-1"


@respx.mock
async def test_root_domain_matches_at_sign(session):
    domain = await _make_domain(session)
    dc1 = await _make_datacenter(session, "dc1")
    target = await _make_target_with_ips(session, domain, name="@", candidates={dc1: ["1.1.1.1"]})

    respx.get(f"{BASE}/zones/zone-1/dns_records").mock(
        return_value=_cf_response(
            [{"id": "cf-rec-root", "name": "example.com", "type": "A", "content": "1.1.1.1"}]
        )
    )

    async with CloudflareClient(api_token="test-token") as client:
        result = await SyncService(session, client).sync_domain(domain)

    assert result.matched_target_ids == [target.id]


@respx.mock
async def test_unmatched_target_when_cloudflare_has_no_record(session):
    domain = await _make_domain(session)
    dc1 = await _make_datacenter(session, "dc1")
    target = await _make_target_with_ips(
        session, domain, name="missing", candidates={dc1: ["1.1.1.1"]}
    )

    respx.get(f"{BASE}/zones/zone-1/dns_records").mock(return_value=_cf_response([]))

    async with CloudflareClient(api_token="test-token") as client:
        result = await SyncService(session, client).sync_domain(domain)

    assert result.unmatched_target_ids == [target.id]
    assert result.matched_target_ids == []


@respx.mock
async def test_flags_datacenter_mismatch_without_auto_fixing(session):
    domain = await _make_domain(session)
    dc_primary = await _make_datacenter(session, "dc-primary")
    dc_secondary = await _make_datacenter(session, "dc-secondary")

    target = await _make_target_with_ips(
        session,
        domain,
        candidates={dc_primary: ["1.2.3.4"], dc_secondary: ["5.6.7.8"]},
    )
    target.current_datacenter_id = dc_primary.id
    await session.flush()

    # Live content actually matches dc_secondary's IP, not dc_primary's.
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
    assert target.current_datacenter_id == dc_primary.id  # not auto-fixed


@respx.mock
async def test_no_mismatch_when_content_matches_current_datacenter(session):
    domain = await _make_domain(session)
    dc = await _make_datacenter(session, "dc-primary")
    target = await _make_target_with_ips(session, domain, candidates={dc: ["1.2.3.4"]})
    target.current_datacenter_id = dc.id
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
async def test_load_balanced_target_fully_matched(session):
    """Two simultaneous A records for one target -- both IPs must be live
    for the datacenter to resolve as fully matched."""
    domain = await _make_domain(session)
    dc = await _make_datacenter(session, "dc-primary")
    target = await _make_target_with_ips(
        session, domain, candidates={dc: ["1.1.1.1", "1.1.1.2"]}
    )
    target.current_datacenter_id = dc.id
    await session.flush()

    respx.get(f"{BASE}/zones/zone-1/dns_records").mock(
        return_value=_cf_response(
            [
                {"id": "cf-rec-1", "name": "www.example.com", "type": "A", "content": "1.1.1.1"},
                {"id": "cf-rec-2", "name": "www.example.com", "type": "A", "content": "1.1.1.2"},
            ]
        )
    )

    async with CloudflareClient(api_token="test-token") as client:
        result = await SyncService(session, client).sync_domain(domain)

    assert result.matched_target_ids == [target.id]
    assert result.filled_record_id_count == 2
    assert result.datacenter_mismatches == []


@respx.mock
async def test_load_balanced_target_partially_matched_flagged(session):
    """Only one of two load-balanced IPs is live -- must be flagged as a
    mismatch (partial), never silently treated as a clean match."""
    domain = await _make_domain(session)
    dc = await _make_datacenter(session, "dc-primary")
    await _make_target_with_ips(session, domain, candidates={dc: ["1.1.1.1", "1.1.1.2"]})

    respx.get(f"{BASE}/zones/zone-1/dns_records").mock(
        return_value=_cf_response(
            [{"id": "cf-rec-1", "name": "www.example.com", "type": "A", "content": "1.1.1.1"}]
        )
    )

    async with CloudflareClient(api_token="test-token") as client:
        result = await SyncService(session, client).sync_domain(domain)

    assert len(result.datacenter_mismatches) == 1
    assert result.datacenter_mismatches[0].partial is True
    assert result.datacenter_mismatches[0].resolved_datacenter_id is None
