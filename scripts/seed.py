"""Seed domains and datacenters, either from a YAML file or interactively.

Usage:
    python -m scripts.seed --file scripts/seed.example.yaml
    python -m scripts.seed                       # interactive prompts

Matches rows by unique name (domain name / datacenter name): an existing row
is updated in place rather than duplicated, so this is safe to re-run.
"""

import argparse
import asyncio
from pathlib import Path

import yaml
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import async_session_factory
from app.models import Datacenter, DatacenterStatus, Domain


async def upsert_domain(session: AsyncSession, name: str, cloudflare_zone_id: str) -> Domain:
    result = await session.execute(select(Domain).where(Domain.name == name))
    domain = result.scalar_one_or_none()
    if domain is None:
        domain = Domain(name=name, cloudflare_zone_id=cloudflare_zone_id)
        session.add(domain)
        print(f"  + domain {name}")
    else:
        domain.cloudflare_zone_id = cloudflare_zone_id
        print(f"  = domain {name} (updated)")
    return domain


async def upsert_datacenter(
    session: AsyncSession,
    name: str,
    status: str,
    notes: str | None,
) -> Datacenter:
    result = await session.execute(select(Datacenter).where(Datacenter.name == name))
    datacenter = result.scalar_one_or_none()
    if datacenter is None:
        datacenter = Datacenter(name=name, status=DatacenterStatus(status), notes=notes)
        session.add(datacenter)
        print(f"  + datacenter {name} ({status})")
    else:
        datacenter.status = DatacenterStatus(status)
        datacenter.notes = notes
        print(f"  = datacenter {name} (updated)")
    return datacenter


async def seed_from_file(path: Path) -> None:
    data = yaml.safe_load(path.read_text()) or {}
    async with async_session_factory() as session:
        for d in data.get("domains", []):
            await upsert_domain(session, d["name"], d["cloudflare_zone_id"])
        for dc in data.get("datacenters", []):
            await upsert_datacenter(
                session,
                dc["name"],
                dc.get("status", "active"),
                dc.get("notes"),
            )
        await session.commit()


def _prompt(label: str, default: str = "") -> str:
    suffix = f" [{default}]" if default else ""
    value = input(f"{label}{suffix}: ").strip()
    return value or default


async def seed_interactive() -> None:
    async with async_session_factory() as session:
        print("Domains (blank name to stop)")
        while True:
            name = _prompt("  domain name")
            if not name:
                break
            zone_id = _prompt("  cloudflare_zone_id", "PLACEHOLDER_ZONE_ID")
            await upsert_domain(session, name, zone_id)

        print("Datacenters (blank name to stop)")
        while True:
            name = _prompt("  datacenter name")
            if not name:
                break
            status = _prompt("  status (active|standby|disabled)", "active")
            notes = _prompt("  notes") or None
            await upsert_datacenter(session, name, status, notes)

        await session.commit()
    print("Done.")


def main() -> None:
    parser = argparse.ArgumentParser(description="Seed domains and datacenters.")
    parser.add_argument(
        "--file", type=Path, help="YAML file with 'domains' and 'datacenters' lists"
    )
    args = parser.parse_args()

    if args.file:
        asyncio.run(seed_from_file(args.file))
    else:
        asyncio.run(seed_interactive())


if __name__ == "__main__":
    main()
