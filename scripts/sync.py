"""Reconcile DnsTarget rows against what Cloudflare actually reports, via
SyncService. Fills in missing cloudflare_record_id values and prints any
datacenter mismatches -- it never auto-fixes a mismatch (see
SyncService.sync_domain's docstring / CLAUDE.md's golden rule).

Usage:
    python -m scripts.sync                    # every domain
    python -m scripts.sync --domain example.com
"""

import argparse
import asyncio

from sqlalchemy import select

from app.cloudflare.client import CloudflareApiError, CloudflareClient
from app.db.session import async_session_factory
from app.models import Domain
from app.services.sync_service import SyncService


async def sync_all(domain_name: str | None = None) -> None:
    async with async_session_factory() as session, CloudflareClient() as client:
        stmt = select(Domain)
        if domain_name:
            stmt = stmt.where(Domain.name == domain_name)
        result = await session.execute(stmt)
        domains = result.scalars().all()

        if not domains:
            suffix = f" matching {domain_name!r}" if domain_name else ""
            print(f"No domain found{suffix}.")
            return

        for domain in domains:
            print(f"Syncing {domain.name} (zone {domain.cloudflare_zone_id})...")
            try:
                sync_result = await SyncService(session, client).sync_domain(domain)
            except CloudflareApiError as exc:
                print(f"  ERROR: Cloudflare API call failed: {exc}")
                continue

            print(f"  matched: {len(sync_result.matched_target_ids)}")
            print(f"  filled cloudflare_record_id: {len(sync_result.filled_record_id_target_ids)}")
            print(f"  unmatched (no Cloudflare record found): {len(sync_result.unmatched_target_ids)}")
            if sync_result.unmatched_target_ids:
                print(f"    target ids: {sync_result.unmatched_target_ids}")
            print(f"  datacenter mismatches (NOT auto-fixed): {len(sync_result.datacenter_mismatches)}")
            for mismatch in sync_result.datacenter_mismatches:
                print(
                    f"    - {mismatch.fqdn}: DB says datacenter #{mismatch.expected_datacenter_id}, "
                    f"Cloudflare content {mismatch.cloudflare_content!r} resolves to "
                    f"datacenter #{mismatch.resolved_datacenter_id}"
                )

        await session.commit()


def main() -> None:
    parser = argparse.ArgumentParser(description="Sync DnsTargets against Cloudflare.")
    parser.add_argument("--domain", help="Only sync this domain (by name); default: all domains")
    args = parser.parse_args()
    asyncio.run(sync_all(args.domain))


if __name__ == "__main__":
    main()
