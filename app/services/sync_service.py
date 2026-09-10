"""Reconciles DnsTarget rows against what Cloudflare actually reports.

Fills in missing cloudflare_record_id values by matching name+type, and
flags (without auto-fixing) any DnsTarget whose current_datacenter_id
doesn't match the datacenter Cloudflare's record content resolves to -- e.g.
because someone changed the record by hand in the Cloudflare dashboard.
"""

from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.cloudflare.client import CloudflareClient
from app.models import Datacenter, DnsTarget, Domain


def target_fqdn(name: str, domain_name: str) -> str:
    return domain_name if name == "@" else f"{name}.{domain_name}"


@dataclass
class DatacenterMismatch:
    dns_target_id: int
    fqdn: str
    cloudflare_content: str
    expected_datacenter_id: int | None
    resolved_datacenter_id: int | None


@dataclass
class SyncResult:
    domain_id: int
    total_cloudflare_records: int = 0
    matched_target_ids: list[int] = field(default_factory=list)
    unmatched_target_ids: list[int] = field(default_factory=list)
    filled_record_id_target_ids: list[int] = field(default_factory=list)
    datacenter_mismatches: list[DatacenterMismatch] = field(default_factory=list)


class SyncService:
    def __init__(self, session: AsyncSession, cloudflare_client: CloudflareClient):
        self._session = session
        self._client = cloudflare_client

    async def sync_domain(self, domain: Domain) -> SyncResult:
        cf_records = await self._client.list_dns_records(domain.cloudflare_zone_id)
        cf_by_key = {(r["name"].lower(), r["type"]): r for r in cf_records}

        targets_result = await self._session.execute(
            select(DnsTarget).where(DnsTarget.domain_id == domain.id)
        )
        targets = targets_result.scalars().all()

        datacenters_result = await self._session.execute(select(Datacenter))
        datacenter_by_ip = {
            dc.ip_address: dc for dc in datacenters_result.scalars().all() if dc.ip_address
        }

        result = SyncResult(domain_id=domain.id, total_cloudflare_records=len(cf_records))

        for target in targets:
            fqdn = target_fqdn(target.name, domain.name)
            cf_record = cf_by_key.get((fqdn.lower(), target.record_type.value))
            if cf_record is None:
                result.unmatched_target_ids.append(target.id)
                continue

            result.matched_target_ids.append(target.id)

            if target.cloudflare_record_id is None:
                target.cloudflare_record_id = cf_record["id"]
                result.filled_record_id_target_ids.append(target.id)

            resolved_datacenter = datacenter_by_ip.get(cf_record["content"])
            resolved_datacenter_id = resolved_datacenter.id if resolved_datacenter else None

            if resolved_datacenter_id != target.current_datacenter_id:
                result.datacenter_mismatches.append(
                    DatacenterMismatch(
                        dns_target_id=target.id,
                        fqdn=fqdn,
                        cloudflare_content=cf_record["content"],
                        expected_datacenter_id=target.current_datacenter_id,
                        resolved_datacenter_id=resolved_datacenter_id,
                    )
                )

        await self._session.flush()
        return result
