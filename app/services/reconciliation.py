"""Shared core for matching a DnsTarget's candidate IPs (1..N per
datacenter) against Cloudflare's live DNS records for that name+type.

Used both by SyncService (already-seeded DnsTarget rows) and by the CSV
topology importer (rows that don't exist as DnsTarget rows yet) -- the
matching logic is identical either way, only where the candidate IPs come
from differs.
"""

from dataclasses import dataclass


@dataclass
class SlotMatch:
    slot_index: int
    ip_address: str
    cloudflare_record_id: str | None  # None if no live record currently has this content


def reconcile_against_live_records(
    candidate_ips_by_datacenter: dict[int, list[str]],
    live_records: list[dict],
) -> dict[int, list[SlotMatch]]:
    """For each datacenter's ordered candidate IP list, find a live record
    (by exact content match) for each slot. Each live record is claimed by
    at most one slot across all datacenters, so a coincidentally-shared IP
    between two datacenters' candidate lists can't double-match the same
    live record."""
    available = list(live_records)
    result: dict[int, list[SlotMatch]] = {}

    for datacenter_id, ips in candidate_ips_by_datacenter.items():
        slot_matches = []
        for slot_index, ip in enumerate(ips):
            match_idx = next((i for i, r in enumerate(available) if r["content"] == ip), None)
            if match_idx is not None:
                record = available.pop(match_idx)
                slot_matches.append(SlotMatch(slot_index, ip, record["id"]))
            else:
                slot_matches.append(SlotMatch(slot_index, ip, None))
        result[datacenter_id] = slot_matches

    return result


def fully_matched(slot_matches: list[SlotMatch]) -> bool:
    return bool(slot_matches) and all(m.cloudflare_record_id is not None for m in slot_matches)


def partially_matched(slot_matches: list[SlotMatch]) -> bool:
    matched = sum(1 for m in slot_matches if m.cloudflare_record_id is not None)
    return 0 < matched < len(slot_matches)


def resolve_datacenter(
    reconciliation: dict[int, list[SlotMatch]],
) -> int | None:
    """The datacenter whose candidate IPs are all confirmed live, if
    exactly one such datacenter exists. Returns None if none or more than
    one datacenter is fully matched (the latter would mean the same
    content is live under two different datacenters' candidate lists,
    which should never happen with a sane topology -- callers should treat
    that as a data problem to investigate, not silently pick one)."""
    fully = [dc_id for dc_id, matches in reconciliation.items() if fully_matched(matches)]
    return fully[0] if len(fully) == 1 else None
