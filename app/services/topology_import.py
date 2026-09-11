"""CSV-driven topology import: read the authoritative desired DNS topology,
cross-check it against live Cloudflare records, and report matched/
not-matched/partial/ambiguous rows -- read-only. apply_topology() (never
invoked automatically by this module) does the actual Domain/DnsTarget/
TargetDatacenterIp/SwitchGroup writes for cleanly-matched rows only, after
a human has reviewed the report.

CSV shape: one row per *candidate IP*, not one row per target -- a
load-balanced target has several consecutive rows, each contributing one
IP per datacenter column present. Row order within a group is slot order.
Columns after (domain, subdomain, record_type) are positional and
variable-length:

    domain,subdomain,record_type[,service][,<dc1_ip>[,<dc2_ip>]]

`service` is present only when the field right after record_type is not a
bare IPv4 address. Confirmed against live Cloudflare data: when present,
`service` is the *actual DNS hostname* (e.g. subdomain "l2tp" + service
"srv3" -> the real record is srv3.wanpire.net, not l2tp.wanpire.net) --
`subdomain` in that case is a grouping/protocol label, not part of the
DNS name at all. Rows are grouped into one target by (domain, DNS name,
record_type) -- the DNS name being `service` when present, else
`subdomain` directly. Switch-group membership is derived from `subdomain`
regardless, so this format stays modular: a new subdomain automatically
gets its own switch group with no code change, and two different
`service` values under the same `subdomain` (e.g. srv3 and srv4, both
"l2tp") correctly become two separate switchable targets in the same
group, not one target with double the slots. A row may have only one IP
column at all (e.g. a domain with no second datacenter configured yet).
"""

import csv
import re
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

# Subdomains excluded entirely -- constant-value records the operator does
# not want touched by this tool at all.
OUT_OF_SCOPE_SUBDOMAINS = {"admin"}

# These subdomains all belong to one combined "Prime" switch group rather
# than each getting their own -- the four georouted location variants plus
# the main prime pointer itself.
PRIME_GROUP_SUBDOMAINS = {"nl", "tr", "uk", "us", "prime"}
PRIME_GROUP_NAME = "Prime"

# Display-name overrides for well-known acronym subdomains; anything else
# not listed here just gets its subdomain capitalized as its group name --
# a new service subdomain needs no code change to get its own switch group.
GROUP_DISPLAY_NAMES = {"l2tp": "L2TP", "sstp": "SSTP"}

_IPV4_RE = re.compile(r"^\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}$")


def _looks_like_ip(value: str) -> bool:
    return bool(_IPV4_RE.match(value))


def group_name_for_subdomain(subdomain: str) -> str | None:
    """None means "not part of any switch group" -- still imported as a
    DnsTarget (unless also out of scope), just not bulk-switchable."""
    key = subdomain.lower()
    if key in OUT_OF_SCOPE_SUBDOMAINS:
        return None
    if key in PRIME_GROUP_SUBDOMAINS:
        return PRIME_GROUP_NAME
    return GROUP_DISPLAY_NAMES.get(key, subdomain.capitalize())


@dataclass
class CsvTarget:
    domain: str
    dns_name: str  # the actual subdomain used in DNS -- service label if present, else subdomain_label
    subdomain_label: str  # the CSV's "subdomain" column; used only to derive group_name
    record_type: str
    farzanegan_ips: list[str] = field(default_factory=list)
    pishgaman_ips: list[str] = field(default_factory=list)
    row_numbers: list[int] = field(default_factory=list)

    @property
    def group_name(self) -> str | None:
        return group_name_for_subdomain(self.subdomain_label)

    @property
    def row_range(self) -> str:
        if not self.row_numbers:
            return "?"
        return (
            str(self.row_numbers[0])
            if len(self.row_numbers) == 1
            else f"{self.row_numbers[0]}-{self.row_numbers[-1]}"
        )


def parse_csv(path: Path) -> list[CsvTarget]:
    targets: dict[tuple[str, str, str], CsvTarget] = {}
    order: list[tuple[str, str, str]] = []

    with path.open(newline="", encoding="utf-8-sig") as f:
        reader = csv.reader(f)
        next(reader, None)  # header

        for row_number, raw_fields in enumerate(reader, start=2):
            fields = [c.strip() for c in raw_fields]
            if not any(fields):
                continue  # blank line

            domain, subdomain_label, record_type = fields[0], fields[1], fields[2].upper()
            if subdomain_label.lower() in OUT_OF_SCOPE_SUBDOMAINS:
                continue

            rest = fields[3:]
            if rest and _looks_like_ip(rest[0]):
                service_label, ip_fields = None, rest
            else:
                service_label, ip_fields = (rest[0] if rest else None), rest[1:]

            dns_name = service_label if service_label else subdomain_label

            farzanegan_ip = ip_fields[0] if len(ip_fields) >= 1 and ip_fields[0] else None
            pishgaman_ip = ip_fields[1] if len(ip_fields) >= 2 and ip_fields[1] else None

            key = (domain, dns_name, record_type)
            if key not in targets:
                targets[key] = CsvTarget(
                    domain=domain,
                    dns_name=dns_name,
                    subdomain_label=subdomain_label,
                    record_type=record_type,
                )
                order.append(key)
            csv_target = targets[key]
            if farzanegan_ip:
                csv_target.farzanegan_ips.append(farzanegan_ip)
            if pishgaman_ip:
                csv_target.pishgaman_ips.append(pishgaman_ip)
            csv_target.row_numbers.append(row_number)

    return [targets[k] for k in order]


@dataclass
class RowReport:
    row: CsvTarget
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
    session: AsyncSession, client: CloudflareClient, csv_targets: list[CsvTarget]
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

    for csv_target in csv_targets:
        domain = domains_by_name.get(csv_target.domain.lower())
        if domain is None:
            reports.append(
                RowReport(
                    row=csv_target,
                    fqdn=f"{csv_target.dns_name}.{csv_target.domain}",
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

        fqdn = target_fqdn(csv_target.dns_name, domain.name)
        live_records = [
            r
            for r in live_by_zone[domain.cloudflare_zone_id]
            if r["name"].lower() == fqdn.lower() and r["type"] == csv_target.record_type
        ]

        candidates = {
            farzanegan.id: csv_target.farzanegan_ips,
            pishgaman.id: csv_target.pishgaman_ips,
        }
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
                row=csv_target,
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
        f"{'rows':>8}  {'domain':<14}{'dns_name':<10}{'type':<6}{'group':<8}"
        f"{'status':<16}{'resolved':<12}"
    )
    lines = [header, "-" * len(header)]
    for r in reports:
        lines.append(
            f"{r.row.row_range:>8}  {r.row.domain:<14}{r.row.dns_name:<10}"
            f"{r.row.record_type:<6}{(r.row.group_name or '-'):<8}{r.status:<16}"
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
        lines.append(
            f"\n=== rows {r.row.row_range}: {r.fqdn} ({r.row.record_type}) [{r.status}] ==="
        )
        lines.append(f"  farzanegan candidates: {r.row.farzanegan_ips}")
        for m in r.farzanegan_matches:
            found = f"LIVE record {m.cloudflare_record_id}" if m.cloudflare_record_id else "NOT FOUND"
            lines.append(f"    slot {m.slot_index}: {m.ip_address} -> {found}")
        lines.append(f"  pishgaman candidates: {r.row.pishgaman_ips or '(none configured)'}")
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
    applied_target_ids_by_group: dict[str, list[int]] = field(default_factory=dict)
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
                DnsTarget.name == r.row.dns_name,
                DnsTarget.record_type == RecordType(r.row.record_type),
            )
        )
        target = existing.scalar_one_or_none()
        if target is None:
            target = DnsTarget(
                domain_id=domain.id,
                name=r.row.dns_name,
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

        if r.row.group_name:
            result.applied_target_ids_by_group.setdefault(r.row.group_name, []).append(target.id)

    await session.flush()
    return result


async def create_switch_groups(
    session: AsyncSession, applied_target_ids_by_group: dict[str, list[int]]
) -> list[SwitchGroup]:
    """Group names come entirely from applied_target_ids_by_group's keys
    (derived from subdomain names during apply_topology) -- no hardcoded
    list, so a new subdomain in a future CSV gets its own group with no
    code change here."""
    created: list[SwitchGroup] = []
    all_target_ids: list[int] = []

    for name, target_ids in applied_target_ids_by_group.items():
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
