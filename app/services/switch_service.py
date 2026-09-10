"""Single and bulk DNS datacenter switching, with dry-run plans and rollback.

Golden rule: no DNS write happens without first computing a diff. plan_*
never calls Cloudflare -- it diffs purely against DB state (current
datacenter's ip_address -> target datacenter's ip_address). execute_*
always re-derives its own plan internally rather than trusting a
caller-supplied target, then checks Cloudflare's live record content before
writing, so an already-correct record is a no-op (still logged).
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
)
from app.services.sync_service import target_fqdn

logger = logging.getLogger(__name__)


class SwitchValidationError(Exception):
    """Raised when a switch can't even be attempted (bad input, unsynced target, etc)."""


@dataclass
class SwitchPlan:
    dns_target_id: int
    fqdn: str
    record_type: str
    cloudflare_record_id: str
    proxied: bool
    current_datacenter_id: int | None
    current_content: str | None
    target_datacenter_id: int
    new_content: str
    no_op: bool


@dataclass
class SwitchExecutionResult:
    dns_target_id: int
    success: bool
    skipped: bool
    audit_log_entry: AuditLogEntry | None
    error_message: str | None = None


@dataclass
class BulkSwitchExecutionSummary:
    switch_group_id: int
    target_datacenter_id: int
    results: list[SwitchExecutionResult] = field(default_factory=list)

    @property
    def succeeded(self) -> list[SwitchExecutionResult]:
        return [r for r in self.results if r.success]

    @property
    def failed(self) -> list[SwitchExecutionResult]:
        return [r for r in self.results if not r.success]


class SwitchService:
    def __init__(self, session: AsyncSession, cloudflare_client: CloudflareClient):
        self._session = session
        self._client = cloudflare_client

    async def plan_single_switch(self, dns_target_id: int, target_datacenter_id: int) -> SwitchPlan:
        target = await self._session.get(DnsTarget, dns_target_id)
        if target is None:
            raise SwitchValidationError(f"DnsTarget {dns_target_id} not found")

        target_dc = await self._session.get(Datacenter, target_datacenter_id)
        if target_dc is None:
            raise SwitchValidationError(f"Datacenter {target_datacenter_id} not found")
        if not target_dc.ip_address:
            raise SwitchValidationError(
                f"Datacenter '{target_dc.name}' has no ip_address configured"
            )
        if target.cloudflare_record_id is None:
            raise SwitchValidationError(
                f"DnsTarget {dns_target_id} has not been synced with Cloudflare yet "
                "(no cloudflare_record_id) -- run a sync first"
            )

        domain = await self._session.get(Domain, target.domain_id)
        fqdn = target_fqdn(target.name, domain.name)

        current_dc = None
        if target.current_datacenter_id is not None:
            current_dc = await self._session.get(Datacenter, target.current_datacenter_id)
        current_content = current_dc.ip_address if current_dc else None

        return SwitchPlan(
            dns_target_id=target.id,
            fqdn=fqdn,
            record_type=target.record_type.value,
            cloudflare_record_id=target.cloudflare_record_id,
            proxied=target.proxied,
            current_datacenter_id=target.current_datacenter_id,
            current_content=current_content,
            target_datacenter_id=target_dc.id,
            new_content=target_dc.ip_address,
            no_op=(current_content == target_dc.ip_address),
        )

    async def plan_bulk_switch(
        self, switch_group_id: int, target_datacenter_id: int
    ) -> list[SwitchPlan]:
        result = await self._session.execute(
            select(SwitchGroup)
            .where(SwitchGroup.id == switch_group_id)
            .options(selectinload(SwitchGroup.members))
        )
        group = result.scalar_one_or_none()
        if group is None:
            raise SwitchValidationError(f"SwitchGroup {switch_group_id} not found")

        return [
            await self.plan_single_switch(member.dns_target_id, target_datacenter_id)
            for member in group.members
        ]

    async def execute_single_switch(
        self,
        dns_target_id: int,
        target_datacenter_id: int,
        actor: str,
        *,
        action_type: ActionType = ActionType.single,
        switch_group_id: int | None = None,
    ) -> SwitchExecutionResult:
        # Re-derive the plan internally -- never trust a caller-supplied diff.
        plan = await self.plan_single_switch(dns_target_id, target_datacenter_id)

        target = await self._session.get(DnsTarget, dns_target_id)
        domain = await self._session.get(Domain, target.domain_id)

        live_record = await self._client.get_dns_record(
            domain.cloudflare_zone_id, plan.cloudflare_record_id
        )
        live_content = live_record.get("content")

        error_message: str | None = None
        skipped = live_content == plan.new_content
        if not skipped:
            try:
                await self._client.update_dns_record(
                    domain.cloudflare_zone_id,
                    plan.cloudflare_record_id,
                    content=plan.new_content,
                    proxied=plan.proxied,
                )
            except CloudflareApiError as exc:
                error_message = str(exc)

        success = error_message is None
        if success:
            target.current_datacenter_id = target_datacenter_id

        entry = AuditLogEntry(
            actor=actor,
            action_type=action_type,
            dns_target_id=target.id,
            switch_group_id=switch_group_id,
            previous_datacenter_id=plan.current_datacenter_id,
            new_datacenter_id=target_datacenter_id,
            cloudflare_record_id=plan.cloudflare_record_id,
            status=AuditStatus.success if success else AuditStatus.failed,
            error_message=error_message,
        )
        self._session.add(entry)
        await self._session.flush()

        return SwitchExecutionResult(
            dns_target_id=target.id,
            success=success,
            skipped=skipped,
            audit_log_entry=entry,
            error_message=error_message,
        )

    async def execute_bulk_switch(
        self, switch_group_id: int, target_datacenter_id: int, actor: str
    ) -> BulkSwitchExecutionSummary:
        plans = await self.plan_bulk_switch(switch_group_id, target_datacenter_id)
        delay = get_settings().bulk_switch_delay_seconds

        summary = BulkSwitchExecutionSummary(
            switch_group_id=switch_group_id, target_datacenter_id=target_datacenter_id
        )

        for i, plan in enumerate(plans):
            try:
                result = await self.execute_single_switch(
                    plan.dns_target_id,
                    target_datacenter_id,
                    actor,
                    action_type=ActionType.bulk,
                    switch_group_id=switch_group_id,
                )
            except SwitchValidationError as exc:
                logger.warning(
                    "bulk switch: target %s could not be executed: %s", plan.dns_target_id, exc
                )
                result = SwitchExecutionResult(
                    dns_target_id=plan.dns_target_id,
                    success=False,
                    skipped=False,
                    audit_log_entry=None,
                    error_message=str(exc),
                )
            summary.results.append(result)

            if i < len(plans) - 1:
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
