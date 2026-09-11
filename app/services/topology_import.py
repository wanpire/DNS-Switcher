"""CSV-driven topology import: read the authoritative desired DNS topology
(domain, subdomain, record_type, service, farzanegan_ips, pishgaman_ips),
cross-check it against live Cloudflare records, and report matched/
not-matched/partial/ambiguous rows -- read-only. apply_topology() (never
invoked automatically by this module) does the actual Domain/DnsTarget/
TargetDatacenterIp/SwitchGroup writes for cleanly-matched rows only, after
a human has reviewed the report.
"""

import csv
from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.cloudflare.client import CloudflareClient
from app.models import Datacenter, DnsTarget, Domain, RecordType, SwitchGroup, SwitchGroupMember, TargetDatacenterIp
from app.services.reconciliation import SlotMatch, fully_matched, reconcile_against_live_records
from app.services.sync_service import target_fqdn

EMERGENCY_GROUP_NAME = "همه‌چیز (اورژانس کامل)"
EMERGENCY_GROUP_DESCRIPTION = "Full emergency failover -- every managed target across all groups"


@dataclass
class CsvRow:
    row_number: int
    domain: str
    subdomain: str
    record_type: str
    service: str
    farzanegan_ips: list[str]
    pishgaman_ips: list[str]


def parse_csv(path: Path) -> list[CsvRow]:
    rows = []
    with path.open(newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for i, raw in enumerate(reader, start=2):  # header is row 1
            rows.append(
                CsvRow(
                    row_number=i,
                    domain=raw["domain"].strip(),
                    subdomain=raw["subdomain"].strip(),
                    record_type=raw["record_type"].strip().upper(),
                    service=raw["service"].strip(),
                    farzanegan_ips=[
                        ip.strip() for ip in raw["farzanegan_ips"].split(",") if ip.strip()
                    ],
                    pishgaman_ips=[
                        ip.strip() for ip in raw["pishgaman_ips"].split(",") if ip.strip()
                    ],
                )
            )
    return rows


@dataclass
class RowReport:
    row: CsvRow
    fqdn: str
    live_records: list[dict]
    farzanegan_datacenter_id: int
    pishgaman_datacenter_id: int
    farzanegan_matches: list[SlotMatch]
    pishgaman_matches: list[SlotMatch]
    unclaimed_live_records: list[dict]
    status: str  # matched | not_matched | partial | ambiguous | domain_not_found
    resolved_datacenter_id: int | None
    resolved_datacenter_name: str | None


async def build_reconciliation_report(
    session: AsyncSession, client: CloudflareClient, csv_rows: list[CsvRow]
) -> list[RowReport]:
    dc_result = await session.execute(select(Datacenter))
    datacenters = {dc.name.lower(): dc for dc in dc_result.scalars().all()}
    missing_dcs = {"farzanegan", "pishgaman"} - datacenters.keys()
    if missing_dcs:
        raise ValueError(
            f"Datacenter(s) not found: {sorted(missing_dcs)} -- seed them first "
            "(scripts/seed.py)"
        )
    farzanegan = datacenters["farzanegan"]
    pishgaman = datacenters["pishgaman"]

    domain_result = await session.execute(select(Domain))
    domains_by_name = {d.name.lower(): d for d in domain_result.scalars().all()}

    live_by_zone: dict[str, list[dict]] = {}
    reports: list[RowReport] = []

    for row in csv_rows:
        domain = domains_by_name.get(row.domain.lower())
        if domain is None:
            reports.append(
                RowReport(
                    row=row,
                    fqdn=f"{row.subdomain}.{row.domain}",
                    live_records=[],
                    farzanegan_datacenter_id=farzanegan.id,
                    pishgaman_datacenter_id=pishgaman.id,
                    farzanegan_matches=[],
                    pishgaman_matches=[],
                    unclaimed_live_records=[],
                    status="domain_not_found",
                    resolved_datacenter_id=None,
                    resolved_datacenter_name=None,
                )
            )
            continue

        if domain.cloudflare_zone_id not in live_by_zone:
            live_by_zone[domain.cloudflare_zone_id] = await client.list_dns_records(
                domain.cloudflare_zone_id
            )

        fqdn = target_fqdn(row.subdomain, domain.name)
        live_records = [
            r
            for r in live_by_zone[domain.cloudflare_zone_id]
            if r["name"].lower() == fqdn.lower() and r["type"] == row.record_type
        ]

        candidates = {farzanegan.id: row.farzanegan_ips, pishgaman.id: row.pishgaman_ips}
        reconciliation = reconcile_against_live_records(candidates, live_records)
        farzanegan_matches = reconciliation[farzanegan.id]
        pishgaman_matches = reconciliation[pishgaman.id]

        claimed_ids = {
            m.cloudflare_record_id
            for m in farzanegan_matches + pishgaman_matches
            if m.cloudflare_record_id
        }
        unclaimed = [r for r in live_records if r["id"] not in claimed_ids]

        fz_full = fully_matched(farzanegan_matches)
        pg_full = fully_matched(pishgaman_matches)

        if not live_records:
            status, resolved_id, resolved_name = "not_matched", None, None
        elif fz_full and pg_full:
            status, resolved_id, resolved_name = "ambiguous", None, None
        elif fz_full:
            status, resolved_id, resolved_name = "matched", farzanegan.id, farzanegan.name
        elif pg_full:
            status, resolved_id, resolved_name = "matched", pishgaman.id, pishgaman.name
        else:
            status, resolved_id, resolved_name = "partial", None, None

        reports.append(
            RowReport(
                row=row,
                fqdn=fqdn,
                live_records=live_records,
                farzanegan_datacenter_id=farzanegan.id,
                pishgaman_datacenter_id=pishgaman.id,
                farzanegan_matches=farzanegan_matches,
                pishgaman_matches=pishgaman_matches,
                unclaimed_live_records=unclaimed,
                status=status,
                resolved_datacenter_id=resolved_id,
                resolved_datacenter_name=resolved_name,
            )
        )

    return reports


def format_report_table(reports: list[RowReport]) -> str:
    header = (
        f"{'row':>4}  {'domain':<14}{'subdomain':<12}{'type':<6}{'service':<8}"
        f"{'status':<16}{'resolved':<12}"
    )
    lines = [header, "-" * len(header)]
    for r in reports:
        lines.append(
            f"{r.row.row_number:>4}  {r.row.domain:<14}{r.row.subdomain:<12}"
            f"{r.row.record_type:<6}{r.row.service:<8}{r.status:<16}"
            f"{r.resolved_datacenter_name or '-':<12}"
        )
    counts: dict[str, int] = {}
    for r in reports:
        counts[r.status] = counts.get(r.status, 0) + 1
    lines.append("")
    lines.append("Summary: " + ", ".join(f"{k}={v}" for k, v in sorted(counts.items())))
    return "\n".join(lines)


def format_report_detail(reports: list[RowReport]) -> str:
    lines = []
    for r in reports:
        if r.status == "matched":
            continue
        lines.append(f"\n=== row {r.row.row_number}: {r.fqdn} ({r.row.record_type}) [{r.status}] ===")
        lines.append(f"  farzanegan candidates: {r.row.farzanegan_ips}")
        for m in r.farzanegan_matches:
            found = f"LIVE record {m.cloudflare_record_id}" if m.cloudflare_record_id else "NOT FOUND"
            lines.append(f"    slot {m.slot_index}: {m.ip_address} -> {found}")
        lines.append(f"  pishgaman candidates: {r.row.pishgaman_ips}")
        for m in r.pishgaman_matches:
            found = f"LIVE record {m.cloudflare_record_id}" if m.cloudflare_record_id else "NOT FOUND"
            lines.append(f"    slot {m.slot_index}: {m.ip_address} -> {found}")
        if r.unclaimed_live_records:
            lines.append("  unexpected live records (match neither list):")
            for rec in r.unclaimed_live_records:
                lines.append(f"    id={rec['id']} content={rec['content']}")
    return "\n".join(lines)


@dataclass
class ApplyResult:
    applied_target_ids_by_service: dict[str, list[int]] = field(default_factory=dict)
    skipped_rows: list[RowReport] = field(default_factory=list)


async def apply_topology(session: AsyncSession, reports: list[RowReport]) -> ApplyResult:
    """Writes DnsTarget + TargetDatacenterIp rows for every cleanly
    "matched" row. Rows in any other status are skipped (listed in
    ApplyResult.skipped_rows) for manual follow-up -- never guessed at."""
    result = ApplyResult()

    domain_result = await session.execute(select(Domain))
    domains_by_name = {d.name.lower(): d for d in domain_result.scalars().all()}

    for r in reports:
        if r.status != "matched":
            result.skipped_rows.append(r)
            continue

        domain = domains_by_name[r.row.domain.lower()]

        existing = await session.execute(
            select(DnsTarget).where(
                DnsTarget.domain_id == domain.id,
                DnsTarget.name == r.row.subdomain,
                DnsTarget.record_type == RecordType(r.row.record_type),
            )
        )
        target = existing.scalar_one_or_none()
        if target is None:
            target = DnsTarget(
                domain_id=domain.id,
                name=r.row.subdomain,
                record_type=RecordType(r.row.record_type),
                proxied=False,
            )
            session.add(target)
            await session.flush()

        target.current_datacenter_id = r.resolved_datacenter_id

        # Write candidate IPs for BOTH datacenters, not just the resolved
        # one -- the other datacenter's IPs need to already be there, ready
        # for a future switch.
        for datacenter_id, matches in (
            (r.farzanegan_datacenter_id, r.farzanegan_matches),
            (r.pishgaman_datacenter_id, r.pishgaman_matches),
        ):
            for m in matches:
                existing_ip = await session.execute(
                    select(TargetDatacenterIp).where(
                        TargetDatacenterIp.dns_target_id == target.id,
                        TargetDatacenterIp.datacenter_id == datacenter_id,
                        TargetDatacenterIp.slot_index == m.slot_index,
                    )
                )
                ip_row = existing_ip.scalar_one_or_none()
                if ip_row is None:
                    ip_row = TargetDatacenterIp(
                        dns_target_id=target.id,
                        datacenter_id=datacenter_id,
                        slot_index=m.slot_index,
                        ip_address=m.ip_address,
                    )
                    session.add(ip_row)
                ip_row.ip_address = m.ip_address
                ip_row.cloudflare_record_id = m.cloudflare_record_id

        result.applied_target_ids_by_service.setdefault(r.row.service, []).append(target.id)

    await session.flush()
    return result


async def create_switch_groups(
    session: AsyncSession, applied_target_ids_by_service: dict[str, list[int]], group_names: list[str]
) -> list[SwitchGroup]:
    created: list[SwitchGroup] = []
    all_target_ids: list[int] = []

    for name in group_names:
        target_ids = applied_target_ids_by_service.get(name, [])
        if not target_ids:
            continue
        group = await _upsert_switch_group(session, name)
        await _set_group_members(session, group, target_ids)
        created.append(group)
        all_target_ids.extend(target_ids)

    if all_target_ids:
        emergency = await _upsert_switch_group(
            session, EMERGENCY_GROUP_NAME, description=EMERGENCY_GROUP_DESCRIPTION
        )
        await _set_group_members(session, emergency, all_target_ids)
        created.append(emergency)

    await session.flush()
    return created


async def _upsert_switch_group(
    session: AsyncSession, name: str, *, description: str | None = None
) -> SwitchGroup:
    existing = await session.execute(select(SwitchGroup).where(SwitchGroup.name == name))
    group = existing.scalar_one_or_none()
    if group is None:
        group = SwitchGroup(name=name, description=description)
        session.add(group)
        await session.flush()
    elif description is not None:
        group.description = description
    return group


async def _set_group_members(session: AsyncSession, group: SwitchGroup, target_ids: list[int]) -> None:
    existing = await session.execute(
        select(SwitchGroupMember).where(SwitchGroupMember.switch_group_id == group.id)
    )
    for member in existing.scalars().all():
        await session.delete(member)
    await session.flush()

    for position, target_id in enumerate(target_ids):
        session.add(
            SwitchGroupMember(switch_group_id=group.id, dns_target_id=target_id, position=position)
        )
