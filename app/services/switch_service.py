"""Single and bulk DNS datacenter switching, with dry-run plans and rollback.

A DnsTarget can have 1..N simultaneous IPs per datacenter (TargetDatacenterIp,
ordered by slot_index) -- load-balanced targets have more than one. Golden
rule: no DNS write happens without first computing a diff. plan_* never
calls Cloudflare -- it diffs purely against DB state, per slot. execute_*
always re-derives its own plan internally rather than trusting a
caller-supplied target, then checks Cloudflare's live record content before
writing each slot, so an already-correct slot is a no-op (still logged).

When the source and destination datacenter have the same number of IP
slots, a switch reuses each slot's existing Cloudflare record (PATCHing its
content) rather than creating/deleting -- cheaper and preserves the
record's Cloudflare-side history. When slot counts differ, execute_*
refuses by default (SwitchValidationError) rather than silently creating or
deleting records; pass allow_slot_count_mismatch=True to confirm.
"""

import asyncio
import logging
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.cloudflare.client import CloudflareApiError, CloudflareClient
from app.core.config import get_settings
from app.models import (
    ActionType,
    AuditLogEntry,
    AuditStatus,
    Datacenter,
    DnsTarget,
    Domain,
    SwitchGroup,
    TargetDatacenterIp,
)
from app.services.sync_service import target_fqdn

logger = logging.getLogger(__name__)


class SwitchValidationError(Exception):
    """Raised when a switch can't even be attempted (bad input, no configured
    IPs, an unconfirmed slot-count mismatch, etc)."""


@dataclass
class SlotDiff:
    slot_index: int
    current_ip: str | None
    new_ip: str | None
    cloudflare_record_id: str | None  # the live record at this slot in the source datacenter, if any


@dataclass
class SwitchPlan:
    dns_target_id: int
    fqdn: str
    record_type: str
    proxied: bool
    current_datacenter_id: int | None
    target_datacenter_id: int
    slot_diffs: list[SlotDiff]
    slot_count_mismatch: bool
    no_op: bool
    # True when the destination datacenter has no IPs configured for this
    # target at all -- a real, expected state (e.g. a datacenter not yet
    # deployed for a given service), not an error. plan_* never raises for
    # this; execute_* short-circuits without touching Cloudflare or writing
    # an audit entry, since nothing was actually attempted.
    unavailable: bool = False
    unavailable_reason: str | None = None


@dataclass
class SwitchExecutionResult:
    dns_target_id: int
    success: bool
    skipped: bool
    audit_log_entry: AuditLogEntry | None
    slot_results: list[dict] = field(default_factory=list)
    error_message: str | None = None
    unavailable: bool = False


@dataclass
class BulkSwitchExecutionSummary:
    switch_group_id: int
    target_datacenter_id: int
    results: list[SwitchExecutionResult] = field(default_factory=list)

    @property
    def succeeded(self) -> list[SwitchExecutionResult]:
        return [r for r in self.results if r.success]

    @property
    def skipped(self) -> list[SwitchExecutionResult]:
        return [r for r in self.results if r.unavailable]

    @property
    def failed(self) -> list[SwitchExecutionResult]:
        return [r for r in self.results if not r.success and not r.unavailable]


class SwitchService:
    def __init__(self, session: AsyncSession, cloudflare_client: CloudflareClient):
        self._session = session
        self._client = cloudflare_client

    async def _get_slots(
        self, dns_target_id: int, datacenter_id: int
    ) -> list[TargetDatacenterIp]:
        result = await self._session.execute(
            select(TargetDatacenterIp)
            .where(
                TargetDatacenterIp.dns_target_id == dns_target_id,
                TargetDatacenterIp.datacenter_id == datacenter_id,
            )
            .order_by(TargetDatacenterIp.slot_index)
        )
        return list(result.scalars().all())

    async def plan_single_switch(self, dns_target_id: int, target_datacenter_id: int) -> SwitchPlan:
        target = await self._session.get(DnsTarget, dns_target_id)
        if target is None:
            raise SwitchValidationError(f"DnsTarget {dns_target_id} not found")

        target_dc = await self._session.get(Datacenter, target_datacenter_id)
        if target_dc is None:
            raise SwitchValidationError(f"Datacenter {target_datacenter_id} not found")

        domain = await self._session.get(Domain, target.domain_id)
        fqdn = target_fqdn(target.name, domain.name)

        dest_slots = await self._get_slots(dns_target_id, target_datacenter_id)
        if not dest_slots:
            # A real, expected state (e.g. a datacenter not yet deployed for
            # this service) -- never an exception. Callers (bulk execute, the
            # API, the bot) all need to represent this as data, not a 400.
            return SwitchPlan(
                dns_target_id=target.id,
                fqdn=fqdn,
                record_type=target.record_type.value,
                proxied=target.proxied,
                current_datacenter_id=target.current_datacenter_id,
                target_datacenter_id=target_dc.id,
                slot_diffs=[],
                slot_count_mismatch=False,
                no_op=False,
                unavailable=True,
                unavailable_reason=(
                    f"دیتاسنتر {target_dc.name} برای این سرویس هنوز راه‌اندازی نشده"
                ),
            )

        source_slots: list[TargetDatacenterIp] = []
        if target.current_datacenter_id is not None:
            source_slots = await self._get_slots(dns_target_id, target.current_datacenter_id)

        slot_count = max(len(source_slots), len(dest_slots))
        slot_diffs = [
            SlotDiff(
                slot_index=i,
                current_ip=source_slots[i].ip_address if i < len(source_slots) else None,
                new_ip=dest_slots[i].ip_address if i < len(dest_slots) else None,
                cloudflare_record_id=(
                    source_slots[i].cloudflare_record_id if i < len(source_slots) else None
                ),
            )
            for i in range(slot_count)
        ]

        return SwitchPlan(
            dns_target_id=target.id,
            fqdn=fqdn,
            record_type=target.record_type.value,
            proxied=target.proxied,
            current_datacenter_id=target.current_datacenter_id,
            target_datacenter_id=target_dc.id,
            slot_diffs=slot_diffs,
            slot_count_mismatch=(len(source_slots) != len(dest_slots)),
            no_op=(
                sorted(s.ip_address for s in source_slots)
                == sorted(s.ip_address for s in dest_slots)
            ),
        )

    async def _group_member_target_ids(
        self, switch_group_id: int, *, domain_id: int | None = None
    ) -> list[int]:
        result = await self._session.execute(
            select(SwitchGroup)
            .where(SwitchGroup.id == switch_group_id)
            .options(selectinload(SwitchGroup.members))
        )
        group = result.scalar_one_or_none()
        if group is None:
            raise SwitchValidationError(f"SwitchGroup {switch_group_id} not found")

        member_ids = [m.dns_target_id for m in group.members]
        if domain_id is None:
            return member_ids

        in_domain = await self._session.execute(
            select(DnsTarget.id).where(
                DnsTarget.id.in_(member_ids), DnsTarget.domain_id == domain_id
            )
        )
        in_domain_ids = {row[0] for row in in_domain.all()}
        return [tid for tid in member_ids if tid in in_domain_ids]

    async def plan_bulk_switch(
        self, switch_group_id: int, target_datacenter_id: int, *, domain_id: int | None = None
    ) -> list[SwitchPlan]:
        member_ids = await self._group_member_target_ids(switch_group_id, domain_id=domain_id)
        return [
            await self.plan_single_switch(target_id, target_datacenter_id)
            for target_id in member_ids
        ]

    async def _update_slot_bookkeeping(
        self,
        dns_target_id: int,
        source_datacenter_id: int | None,
        dest_datacenter_id: int,
        slot_results: list[dict],
    ) -> None:
        """Moves cloudflare_record_id from the source datacenter's slots to
        the destination's, matching what execute_single_switch actually did
        to each record (reused via PATCH, newly created, or deleted)."""
        source_slots = {
            s.slot_index: s
            for s in (
                await self._get_slots(dns_target_id, source_datacenter_id)
                if source_datacenter_id is not None
                else []
            )
        }
        dest_slots = {s.slot_index: s for s in await self._get_slots(dns_target_id, dest_datacenter_id)}

        for result_entry in slot_results:
            slot_index = result_entry["slot_index"]
            if slot_index in source_slots:
                source_slots[slot_index].cloudflare_record_id = None
            if result_entry["action"] in ("updated", "skipped", "created") and slot_index in dest_slots:
                dest_slots[slot_index].cloudflare_record_id = result_entry["cloudflare_record_id"]

    async def execute_single_switch(
        self,
        dns_target_id: int,
        target_datacenter_id: int,
        actor: str,
        *,
        action_type: ActionType = ActionType.single,
        switch_group_id: int | None = None,
        allow_slot_count_mismatch: bool = False,
    ) -> SwitchExecutionResult:
        # Re-derive the plan internally -- never trust a caller-supplied diff.
        plan = await self.plan_single_switch(dns_target_id, target_datacenter_id)

        if plan.unavailable:
            # Nothing to attempt -- no Cloudflare call, no audit entry (there
            # was no real action taken, same reasoning as a validation
            # failure on a bad ID, just never raised for this specific case).
            return SwitchExecutionResult(
                dns_target_id=dns_target_id,
                success=False,
                skipped=False,
                audit_log_entry=None,
                error_message=plan.unavailable_reason,
                unavailable=True,
            )

        if plan.slot_count_mismatch and not allow_slot_count_mismatch:
            source_count = sum(1 for d in plan.slot_diffs if d.current_ip is not None)
            dest_count = sum(1 for d in plan.slot_diffs if d.new_ip is not None)
            raise SwitchValidationError(
                f"DnsTarget {dns_target_id}: source datacenter has {source_count} IP(s), "
                f"destination has {dest_count} -- this switch would create or delete "
                "records, not just update content. Pass allow_slot_count_mismatch=True "
                "to confirm."
            )

        target = await self._session.get(DnsTarget, dns_target_id)
        domain = await self._session.get(Domain, target.domain_id)

        slot_results: list[dict] = []
        all_ok = True

        for slot_diff in plan.slot_diffs:
            entry: dict = {
                "slot_index": slot_diff.slot_index,
                "ip_address": slot_diff.new_ip,
                "cloudflare_record_id": None,
                "action": None,
                "success": False,
                "error": None,
            }
            try:
                if slot_diff.cloudflare_record_id is not None and slot_diff.new_ip is not None:
                    live_record = await self._client.get_dns_record(
                        domain.cloudflare_zone_id, slot_diff.cloudflare_record_id
                    )
                    entry["cloudflare_record_id"] = slot_diff.cloudflare_record_id
                    if live_record.get("content") == slot_diff.new_ip:
                        entry["action"] = "skipped"
                    else:
                        await self._client.update_dns_record(
                            domain.cloudflare_zone_id,
                            slot_diff.cloudflare_record_id,
                            content=slot_diff.new_ip,
                            proxied=target.proxied,
                        )
                        entry["action"] = "updated"
                elif slot_diff.cloudflare_record_id is not None and slot_diff.new_ip is None:
                    await self._client.delete_dns_record(
                        domain.cloudflare_zone_id, slot_diff.cloudflare_record_id
                    )
                    entry["action"] = "deleted"
                    entry["ip_address"] = slot_diff.current_ip
                elif slot_diff.cloudflare_record_id is None and slot_diff.new_ip is not None:
                    created = await self._client.create_dns_record(
                        domain.cloudflare_zone_id,
                        name=plan.fqdn,
                        type=plan.record_type,
                        content=slot_diff.new_ip,
                        proxied=target.proxied,
                    )
                    entry["action"] = "created"
                    entry["cloudflare_record_id"] = created["id"]
                else:
                    entry["action"] = "skipped"
                entry["success"] = True
            except CloudflareApiError as exc:
                entry["error"] = str(exc)
                entry["success"] = False
                all_ok = False

            slot_results.append(entry)

        if all_ok:
            await self._update_slot_bookkeeping(
                dns_target_id, target.current_datacenter_id, target_datacenter_id, slot_results
            )
            target.current_datacenter_id = target_datacenter_id

        error_message = None
        if not all_ok:
            failed = [r for r in slot_results if not r["success"]]
            error_message = "; ".join(f"slot {r['slot_index']}: {r['error']}" for r in failed)

        audit_entry = AuditLogEntry(
            actor=actor,
            action_type=action_type,
            dns_target_id=target.id,
            switch_group_id=switch_group_id,
            previous_datacenter_id=plan.current_datacenter_id,
            new_datacenter_id=target_datacenter_id,
            slot_results=slot_results,
            status=AuditStatus.success if all_ok else AuditStatus.failed,
            error_message=error_message,
        )
        self._session.add(audit_entry)
        await self._session.flush()

        skipped = all_ok and all(r["action"] == "skipped" for r in slot_results)

        return SwitchExecutionResult(
            dns_target_id=target.id,
            success=all_ok,
            skipped=skipped,
            audit_log_entry=audit_entry,
            slot_results=slot_results,
            error_message=error_message,
        )

    async def execute_bulk_switch(
        self,
        switch_group_id: int,
        target_datacenter_id: int,
        actor: str,
        *,
        domain_id: int | None = None,
    ) -> BulkSwitchExecutionSummary:
        member_ids = await self._group_member_target_ids(switch_group_id, domain_id=domain_id)
        delay = get_settings().bulk_switch_delay_seconds

        summary = BulkSwitchExecutionSummary(
            switch_group_id=switch_group_id, target_datacenter_id=target_datacenter_id
        )

        for i, target_id in enumerate(member_ids):
            try:
                # Slot-count mismatches are never silently allowed in bulk --
                # a member that would create/delete records fails cleanly
                # here and must be handled individually with explicit
                # confirmation, same reasoning as the single-switch default.
                # A target with no IPs configured for the destination
                # datacenter (execute_single_switch's unavailable=True
                # result) is NOT a failure here either -- it's tallied under
                # summary.skipped, not summary.failed.
                result = await self.execute_single_switch(
                    target_id,
                    target_datacenter_id,
                    actor,
                    action_type=ActionType.bulk,
                    switch_group_id=switch_group_id,
                )
            except SwitchValidationError as exc:
                logger.warning("bulk switch: target %s could not be executed: %s", target_id, exc)
                result = SwitchExecutionResult(
                    dns_target_id=target_id,
                    success=False,
                    skipped=False,
                    audit_log_entry=None,
                    error_message=str(exc),
                )
            summary.results.append(result)

            if i < len(member_ids) - 1:
                await asyncio.sleep(delay)

        return summary

    async def rollback(self, audit_log_entry_id: int, actor: str) -> SwitchExecutionResult:
        original = await self._session.get(AuditLogEntry, audit_log_entry_id)
        if original is None:
            raise SwitchValidationError(f"AuditLogEntry {audit_log_entry_id} not found")
        if original.status != AuditStatus.success:
            raise SwitchValidationError(
                f"AuditLogEntry {audit_log_entry_id} is not a successful switch "
                f"(status={original.status.value}); nothing to roll back"
            )
        if original.dns_target_id is None:
            raise SwitchValidationError(
                f"AuditLogEntry {audit_log_entry_id} has no associated DNS target "
                "(it may have been deleted)"
            )
        if original.previous_datacenter_id is None:
            raise SwitchValidationError(
                f"AuditLogEntry {audit_log_entry_id} has no previous datacenter to roll back to"
            )

        result = await self.execute_single_switch(
            original.dns_target_id,
            original.previous_datacenter_id,
            actor,
            action_type=ActionType.rollback,
            switch_group_id=original.switch_group_id,
        )

        if result.audit_log_entry is not None:
            result.audit_log_entry.rollback_of_id = original.id
        if result.success:
            original.status = AuditStatus.rolled_back

        await self._session.flush()
        return result
