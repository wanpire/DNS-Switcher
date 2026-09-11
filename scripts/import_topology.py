"""CSV-driven topology import: cross-checks a CSV of the authoritative
desired DNS topology against live Cloudflare DNS records, and reports
which rows cleanly matched. See app/services/topology_import.py's
docstring for the exact CSV shape (one row per candidate IP, grouped by
(domain, subdomain, record_type); switch-group membership is derived from
the subdomain name, not a column -- fully modular, a new subdomain in a
future CSV needs no code change to get its own switch group).

Always read-only unless --apply is passed. Review the report first --
--apply only writes rows that matched cleanly (status "matched"); anything
else is skipped and listed for manual follow-up, never guessed at.

Usage:
    python -m scripts.import_topology --csv topology.csv           # report only
    python -m scripts.import_topology --csv topology.csv --apply   # write + create switch groups
"""

import argparse
import asyncio
from pathlib import Path

from app.cloudflare.client import CloudflareClient
from app.db.session import async_session_factory
from app.services.topology_import import (
    apply_topology,
    build_reconciliation_report,
    create_switch_groups,
    format_report_detail,
    format_report_table,
    parse_csv,
)


async def run(csv_path: Path, apply: bool) -> None:
    csv_targets = parse_csv(csv_path)
    print(f"Parsed {len(csv_targets)} target(s) from {csv_path}")

    async with async_session_factory() as session, CloudflareClient() as client:
        reports = await build_reconciliation_report(session, client, csv_targets)

        print()
        print(format_report_table(reports))
        detail = format_report_detail(reports)
        if detail:
            print(detail)

        if not apply:
            print(
                "\nRead-only mode (default) -- no DB writes made. Re-run with --apply "
                "once you've reviewed this report."
            )
            return

        result = await apply_topology(session, reports)
        await session.commit()
        applied_count = sum(len(v) for v in result.applied_target_ids_by_group.values())
        print(f"\nApplied {applied_count} target(s).")
        if result.skipped_rows:
            print(
                f"Skipped {len(result.skipped_rows)} row(s) (not cleanly matched) -- "
                "see the detail section above."
            )

        groups = await create_switch_groups(session, result.applied_target_ids_by_group)
        await session.commit()
        print(f"Created/updated {len(groups)} switch group(s): {[g.name for g in groups]}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Import DNS topology from CSV, cross-checked against Cloudflare."
    )
    parser.add_argument("--csv", required=True, type=Path)
    parser.add_argument(
        "--apply", action="store_true", help="Actually write to the DB (default: report only)"
    )
    args = parser.parse_args()
    asyncio.run(run(args.csv, args.apply))


if __name__ == "__main__":
    main()
