import httpx
import respx

from app.cloudflare.client import CloudflareClient
from app.models import Datacenter, Domain
from app.services.topology_import import (
    apply_topology,
    build_reconciliation_report,
    create_switch_groups,
    group_name_for_subdomain,
    parse_csv,
)

BASE = "https://api.cloudflare.com/client/v4"


def _cf_response(records):
    return httpx.Response(200, json={"success": True, "result": records})


async def _make_domain(session, name, zone):
    domain = Domain(name=name, cloudflare_zone_id=zone)
    session.add(domain)
    await session.flush()
    return domain


async def _make_datacenter(session, name):
    dc = Datacenter(name=name)
    session.add(dc)
    await session.flush()
    return dc


# --- parse_csv: real-shaped CSV, variable columns, grouped rows ------------


def test_parse_csv_groups_multi_row_load_balanced_target(tmp_path):
    """subdomain "sstp" + service "ss" -- the real DNS name is "ss"
    (confirmed against live Cloudflare data), "sstp" is only a grouping
    label."""
    csv_path = tmp_path / "topology.csv"
    csv_path.write_text(
        "domain,subdomain,record_type,service,farzanegan_ips,pishgaman_ips\n"
        "wanpire.net,sstp,A,ss,95.142.238.2,195.8.102.83\n"
        "wanpire.net,sstp,A,ss,95.142.238.3,195.8.102.84\n"
    )
    targets = parse_csv(csv_path)
    assert len(targets) == 1
    t = targets[0]
    assert t.domain == "wanpire.net"
    assert t.dns_name == "ss"
    assert t.subdomain_label == "sstp"
    assert t.group_name == "SSTP"
    assert t.record_type == "A"
    assert t.farzanegan_ips == ["95.142.238.2", "95.142.238.3"]
    assert t.pishgaman_ips == ["195.8.102.83", "195.8.102.84"]
    assert t.row_numbers == [2, 3]


def test_parse_csv_handles_row_with_no_service_column(tmp_path):
    csv_path = tmp_path / "topology.csv"
    csv_path.write_text(
        "domain,subdomain,record_type,service,farzanegan_ips,pishgaman_ips\n"
        "wanpire.net,nl,A,95.142.238.7,91.213.151.64\n"
    )
    targets = parse_csv(csv_path)
    assert len(targets) == 1
    t = targets[0]
    assert t.dns_name == "nl"
    assert t.subdomain_label == "nl"
    assert t.farzanegan_ips == ["95.142.238.7"]
    assert t.pishgaman_ips == ["91.213.151.64"]


def test_parse_csv_handles_single_ip_no_second_datacenter(tmp_path):
    """alonet.co has no Pishgaman datacenter configured yet -- rows have
    only one IP column, no service."""
    csv_path = tmp_path / "topology.csv"
    csv_path.write_text(
        "domain,subdomain,record_type,farzanegan_ips\nalonet.co,nl,A,95.142.238.77\n"
    )
    targets = parse_csv(csv_path)
    t = targets[0]
    assert t.farzanegan_ips == ["95.142.238.77"]
    assert t.pishgaman_ips == []


def test_parse_csv_handles_single_ip_with_service(tmp_path):
    csv_path = tmp_path / "topology.csv"
    csv_path.write_text(
        "domain,subdomain,record_type,service,farzanegan_ips\n"
        "alonet.co,l2tp,A,l2,95.142.238.69\n"
        "alonet.co,l2tp,A,l2,95.142.238.70\n"
    )
    targets = parse_csv(csv_path)
    assert len(targets) == 1
    t = targets[0]
    assert t.dns_name == "l2"
    assert t.subdomain_label == "l2tp"
    assert t.farzanegan_ips == ["95.142.238.69", "95.142.238.70"]
    assert t.pishgaman_ips == []


def test_parse_csv_skips_out_of_scope_subdomain(tmp_path):
    csv_path = tmp_path / "topology.csv"
    csv_path.write_text(
        "domain,subdomain,record_type,service,farzanegan_ips,pishgaman_ips\n"
        "wanpire.net,admin,A,admin,193.56.59.68,195.8.102.87\n"
        "wanpire.net,www,A,,1.1.1.1,2.2.2.2\n"
    )
    targets = parse_csv(csv_path)
    assert [t.dns_name for t in targets] == ["www"]


def test_parse_csv_skips_blank_lines(tmp_path):
    csv_path = tmp_path / "topology.csv"
    csv_path.write_text(
        "domain,subdomain,record_type,service,farzanegan_ips,pishgaman_ips\n"
        "wanpire.net,nl,A,95.142.238.7,91.213.151.64\n"
        "\n"
        "alonet.co,nl,A,95.142.238.77\n"
    )
    targets = parse_csv(csv_path)
    assert len(targets) == 2


def test_parse_csv_different_services_under_same_subdomain_are_separate_targets(tmp_path):
    """The real l2tp case: two service labels (srv3, srv4) share the same
    subdomain grouping label AND even the same IP pool (both L2TP boxes
    happen to share infrastructure) -- confirmed against live Cloudflare
    data that srv3.wanpire.net and srv4.wanpire.net are two genuinely
    separate, independently switchable records, NOT one target with
    double the slots. They must stay separate but share a group_name."""
    csv_path = tmp_path / "topology.csv"
    lines = ["domain,subdomain,record_type,service,farzanegan_ips,pishgaman_ips"]
    for label in ("srv3", "srv4"):
        for far, pish in [
            ("95.142.238.2", "195.8.102.83"),
            ("95.142.238.3", "195.8.102.84"),
        ]:
            lines.append(f"wanpire.net,l2tp,A,{label},{far},{pish}")
    csv_path.write_text("\n".join(lines) + "\n")

    targets = parse_csv(csv_path)
    assert len(targets) == 2
    by_name = {t.dns_name: t for t in targets}
    assert set(by_name) == {"srv3", "srv4"}
    for t in targets:
        assert t.group_name == "L2TP"
        assert t.farzanegan_ips == ["95.142.238.2", "95.142.238.3"]
        assert t.pishgaman_ips == ["195.8.102.83", "195.8.102.84"]


# --- group_name_for_subdomain -----------------------------------------------


def test_group_name_prime_cluster():
    for sub in ("nl", "tr", "uk", "us", "prime"):
        assert group_name_for_subdomain(sub) == "Prime"


def test_group_name_known_acronyms():
    assert group_name_for_subdomain("l2tp") == "L2TP"
    assert group_name_for_subdomain("sstp") == "SSTP"


def test_group_name_out_of_scope():
    assert group_name_for_subdomain("admin") is None


def test_group_name_falls_back_to_capitalized_subdomain():
    assert group_name_for_subdomain("open") == "Open"
    assert group_name_for_subdomain("cisco") == "Cisco"
    assert group_name_for_subdomain("newservice") == "Newservice"


# --- build_reconciliation_report -------------------------------------------


@respx.mock
async def test_report_matched_when_farzanegan_ips_all_live(session, tmp_path):
    domain = await _make_domain(session, "wanpire.net", "zone-1")
    await _make_datacenter(session, "Farzanegan")
    await _make_datacenter(session, "Pishgaman")

    csv_path = tmp_path / "t.csv"
    csv_path.write_text(
        "domain,subdomain,record_type,farzanegan_ips,pishgaman_ips\n"
        "wanpire.net,www,A,1.1.1.1,2.2.2.1\n"
    )
    targets = parse_csv(csv_path)

    respx.get(f"{BASE}/zones/zone-1/dns_records").mock(
        return_value=_cf_response(
            [{"id": "cf-1", "name": "www.wanpire.net", "type": "A", "content": "1.1.1.1"}]
        )
    )

    async with CloudflareClient(api_token="test-token") as client:
        reports = await build_reconciliation_report(session, client, targets)

    assert len(reports) == 1
    assert reports[0].status == "matched"
    assert reports[0].resolved_datacenter_name == "Farzanegan"


@respx.mock
async def test_report_matched_with_no_pishgaman_configured(session, tmp_path):
    """alonet.co case: no pishgaman column at all -- must still resolve
    cleanly to Farzanegan, never treated as partial/ambiguous."""
    domain = await _make_domain(session, "alonet.co", "zone-2")
    await _make_datacenter(session, "Farzanegan")
    await _make_datacenter(session, "Pishgaman")

    csv_path = tmp_path / "t.csv"
    csv_path.write_text("domain,subdomain,record_type,farzanegan_ips\nalonet.co,nl,A,9.9.9.9\n")
    targets = parse_csv(csv_path)

    respx.get(f"{BASE}/zones/zone-2/dns_records").mock(
        return_value=_cf_response(
            [{"id": "cf-1", "name": "nl.alonet.co", "type": "A", "content": "9.9.9.9"}]
        )
    )

    async with CloudflareClient(api_token="test-token") as client:
        reports = await build_reconciliation_report(session, client, targets)

    assert reports[0].status == "matched"
    assert reports[0].resolved_datacenter_name == "Farzanegan"


@respx.mock
async def test_report_partial_when_only_some_load_balanced_ips_live(session, tmp_path):
    domain = await _make_domain(session, "wanpire.net", "zone-1")
    await _make_datacenter(session, "Farzanegan")
    await _make_datacenter(session, "Pishgaman")

    csv_path = tmp_path / "t.csv"
    csv_path.write_text(
        "domain,subdomain,record_type,farzanegan_ips,pishgaman_ips\n"
        "wanpire.net,sstp,A,1.1.1.1,2.2.2.1\n"
        "wanpire.net,sstp,A,1.1.1.2,2.2.2.2\n"
    )
    targets = parse_csv(csv_path)

    respx.get(f"{BASE}/zones/zone-1/dns_records").mock(
        return_value=_cf_response(
            [{"id": "cf-1", "name": "sstp.wanpire.net", "type": "A", "content": "1.1.1.1"}]
        )
    )

    async with CloudflareClient(api_token="test-token") as client:
        reports = await build_reconciliation_report(session, client, targets)

    assert reports[0].status == "partial"


@respx.mock
async def test_report_not_matched_when_no_live_records(session, tmp_path):
    domain = await _make_domain(session, "wanpire.net", "zone-1")
    await _make_datacenter(session, "Farzanegan")
    await _make_datacenter(session, "Pishgaman")

    csv_path = tmp_path / "t.csv"
    csv_path.write_text("domain,subdomain,record_type,farzanegan_ips\nwanpire.net,missing,A,1.1.1.1\n")
    targets = parse_csv(csv_path)

    respx.get(f"{BASE}/zones/zone-1/dns_records").mock(return_value=_cf_response([]))

    async with CloudflareClient(api_token="test-token") as client:
        reports = await build_reconciliation_report(session, client, targets)

    assert reports[0].status == "not_matched"


# --- apply_topology + create_switch_groups ----------------------------------


@respx.mock
async def test_apply_topology_writes_matched_targets_and_skips_others(session, tmp_path):
    await _make_domain(session, "wanpire.net", "zone-1")
    await _make_datacenter(session, "Farzanegan")
    await _make_datacenter(session, "Pishgaman")

    csv_path = tmp_path / "t.csv"
    csv_path.write_text(
        "domain,subdomain,record_type,farzanegan_ips,pishgaman_ips\n"
        "wanpire.net,www,A,1.1.1.1,2.2.2.1\n"
        "wanpire.net,missing,A,9.9.9.9,8.8.8.8\n"
    )
    targets = parse_csv(csv_path)

    respx.get(f"{BASE}/zones/zone-1/dns_records").mock(
        return_value=_cf_response(
            [{"id": "cf-1", "name": "www.wanpire.net", "type": "A", "content": "1.1.1.1"}]
        )
    )

    async with CloudflareClient(api_token="test-token") as client:
        reports = await build_reconciliation_report(session, client, targets)
        result = await apply_topology(session, reports)

    assert len(result.skipped_rows) == 1
    assert result.skipped_rows[0].row.dns_name == "missing"
    assert "Www" in result.applied_target_ids_by_group


@respx.mock
async def test_create_switch_groups_derives_groups_dynamically_and_builds_emergency_group(session, tmp_path):
    await _make_domain(session, "wanpire.net", "zone-1")
    await _make_domain(session, "alonet.co", "zone-2")
    await _make_datacenter(session, "Farzanegan")
    await _make_datacenter(session, "Pishgaman")

    csv_path = tmp_path / "t.csv"
    csv_path.write_text(
        "domain,subdomain,record_type,farzanegan_ips,pishgaman_ips\n"
        "wanpire.net,open,A,1.1.1.1,2.2.2.1\n"
        "alonet.co,open,A,3.3.3.1,\n"
    )
    targets = parse_csv(csv_path)

    respx.get(f"{BASE}/zones/zone-1/dns_records").mock(
        return_value=_cf_response(
            [{"id": "cf-1", "name": "open.wanpire.net", "type": "A", "content": "1.1.1.1"}]
        )
    )
    respx.get(f"{BASE}/zones/zone-2/dns_records").mock(
        return_value=_cf_response(
            [{"id": "cf-2", "name": "open.alonet.co", "type": "A", "content": "3.3.3.1"}]
        )
    )

    async with CloudflareClient(api_token="test-token") as client:
        reports = await build_reconciliation_report(session, client, targets)
        result = await apply_topology(session, reports)
        groups = await create_switch_groups(session, result.applied_target_ids_by_group)

    group_names = {g.name for g in groups}
    assert "Open" in group_names
    assert "همه‌چیز (اورژانس کامل)" in group_names
    open_group = next(g for g in groups if g.name == "Open")
    await session.refresh(open_group, attribute_names=["members"])
    assert len(open_group.members) == 2  # spans both domains
