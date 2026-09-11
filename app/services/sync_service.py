"""Reconciles already-seeded DnsTarget rows (with their TargetDatacenterIp
candidates) against what Cloudflare actually reports.

Fills in missing cloudflare_record_id values on TargetDatacenterIp rows by
matching live record content, and flags (without auto-fixing) any
DnsTarget whose current_datacenter_id doesn't match the one datacenter
whose full candidate IP set is confirmed live -- e.g. because someone
changed a record by hand in the Cloudflare dashboard, or a load-balanced
target is only partially cut over.
"""

from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.cloudflare.client import CloudflareClient
from app.models import DnsTarget, Domain, TargetDatacenterIp
from app.services.reconciliation import (
    partially_matched,
    reconcile_against_live_records,
    resolve_datacenter,
)


def target_fqdn(name: str, domain_name: str) -> str:
    return domain_name if name == "@" else f"{name}.{domain_name}"


@dataclass
class DatacenterMismatch:
    dns_target_id: int
    fqdn: str
    expected_datacenter_id: int | None
    resolved_datacenter_id: int | None
    partial: bool  # True if the resolved datacenter matched only some of its slots


@dataclass
class SyncResult:
    domain_id: int
    total_cloudflare_records: int = 0
    matched_target_ids: list[int] = field(default_factory=list)
    unmatched_target_ids: list[int] = field(default_factory=list)
    filled_record_id_count: int = 0
    datacenter_mismatches: list[DatacenterMismatch] = field(default_factory=list)


class SyncService:
    def __init__(self, session: AsyncSession, cloudflare_client: CloudflareClient):
        self._session = session
        self._client = cloudflare_client

    async def sync_domain(self, domain: Domain) -> SyncResult:
        cf_records = await self._client.list_dns_records(domain.cloudflare_zone_id)
        cf_by_key: dict[tuple[str, str], list[dict]] = {}
        for r in cf_records:
            cf_by_key.setdefault((r["name"].lower(), r["type"]), []).append(r)

        targets_result = await self._session.execute(
            select(DnsTarget)
            .where(DnsTarget.domain_id == domain.id)
            .options(selectinload(DnsTarget.datacenter_ips))
        )
        targets = targets_result.scalars().all()

        result = SyncResult(domain_id=domain.id, total_cloudflare_records=len(cf_records))

        for target in targets:
            fqdn = target_fqdn(target.name, domain.name)
            live_records = cf_by_key.get((fqdn.lower(), target.record_type.value), [])
            if not live_records:
                result.unmatched_target_ids.append(target.id)
                continue

            result.matched_target_ids.append(target.id)

            candidates_by_dc_slot: dict[int, dict[int, TargetDatacenterIp]] = {}
            for row in target.datacenter_ips:
                candidates_by_dc_slot.setdefault(row.datacenter_id, {})[row.slot_index] = row
            candidates: dict[int, list[str]] = {
                dc_id: [slots[i].ip_address for i in sorted(slots)]
                for dc_id, slots in candidates_by_dc_slot.items()
            }

            reconciliation = reconcile_against_live_records(candidates, live_records)

            for dc_id, slot_matches in reconciliation.items():
                slots = candidates_by_dc_slot[dc_id]
                for match in slot_matches:
                    row = slots.get(match.slot_index)
                    if row is not None and row.cloudflare_record_id != match.cloudflare_record_id:
                        row.cloudflare_record_id = match.cloudflare_record_id
                        result.filled_record_id_count += 1

            resolved_dc_id = resolve_datacenter(reconciliation)
            is_partial = resolved_dc_id is None and any(
                partially_matched(m) for m in reconciliation.values()
            )

            if resolved_dc_id != target.current_datacenter_id or is_partial:
                result.datacenter_mismatches.append(
                    DatacenterMismatch(
                        dns_target_id=target.id,
                        fqdn=fqdn,
                        expected_datacenter_id=target.current_datacenter_id,
                        resolved_datacenter_id=resolved_dc_id,
                        partial=is_partial,
                    )
                )

        await self._session.flush()
        return result
