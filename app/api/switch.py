from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.api.deps import get_cloudflare_client, verify_internal_secret
from app.cloudflare.client import CloudflareClient
from app.db.session import get_db
from app.models import AuditLogEntry, Datacenter, DnsTarget, Domain, SwitchGroup
from app.schemas.switch import (
    AuditLogPageOut,
    BulkSwitchExecuteRequest,
    BulkSwitchExecutionOut,
    BulkSwitchPlanRequest,
    DatacenterOut,
    DnsTargetOut,
    DomainOut,
    RollbackRequest,
    SingleSwitchExecuteRequest,
    SingleSwitchPlanRequest,
    SwitchExecutionOut,
    SwitchGroupOut,
    SwitchPlanOut,
)
from app.services.switch_service import SwitchService, SwitchValidationError

router = APIRouter(dependencies=[Depends(verify_internal_secret)])


def _to_execution_out(result) -> SwitchExecutionOut:
    return SwitchExecutionOut(
        dns_target_id=result.dns_target_id,
        success=result.success,
        skipped=result.skipped,
        error_message=result.error_message,
        audit_log_entry_id=result.audit_log_entry.id if result.audit_log_entry else None,
        slot_results=result.slot_results,
        unavailable=result.unavailable,
    )


@router.get("/domains", response_model=list[DomainOut])
async def list_domains(session: AsyncSession = Depends(get_db)):
    result = await session.execute(select(Domain))
    return result.scalars().all()


@router.get("/targets", response_model=list[DnsTargetOut])
async def list_targets(
    domain_id: int | None = None,
    session: AsyncSession = Depends(get_db),
):
    stmt = select(DnsTarget)
    if domain_id is not None:
        stmt = stmt.where(DnsTarget.domain_id == domain_id)
    result = await session.execute(stmt)
    return result.scalars().all()


@router.get("/groups", response_model=list[SwitchGroupOut])
async def list_groups(session: AsyncSession = Depends(get_db)):
    result = await session.execute(
        select(SwitchGroup).options(selectinload(SwitchGroup.members))
    )
    groups = result.scalars().unique().all()
    return [
        SwitchGroupOut(
            id=g.id,
            name=g.name,
            description=g.description,
            member_dns_target_ids=[m.dns_target_id for m in g.members],
        )
        for g in groups
    ]


@router.get("/datacenters", response_model=list[DatacenterOut])
async def list_datacenters(session: AsyncSession = Depends(get_db)):
    result = await session.execute(select(Datacenter))
    return result.scalars().all()


@router.post("/switch/single/plan", response_model=SwitchPlanOut)
async def plan_single_switch(
    body: SingleSwitchPlanRequest,
    session: AsyncSession = Depends(get_db),
    cf: CloudflareClient = Depends(get_cloudflare_client),
):
    service = SwitchService(session, cf)
    try:
        plan = await service.plan_single_switch(body.dns_target_id, body.target_datacenter_id)
    except SwitchValidationError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    return SwitchPlanOut.model_validate(plan)


@router.post("/switch/single/execute", response_model=SwitchExecutionOut)
async def execute_single_switch(
    body: SingleSwitchExecuteRequest,
    session: AsyncSession = Depends(get_db),
    cf: CloudflareClient = Depends(get_cloudflare_client),
):
    service = SwitchService(session, cf)
    try:
        result = await service.execute_single_switch(
            body.dns_target_id,
            body.target_datacenter_id,
            body.actor,
            allow_slot_count_mismatch=body.allow_slot_count_mismatch,
        )
    except SwitchValidationError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    return _to_execution_out(result)


@router.post("/switch/bulk/plan", response_model=list[SwitchPlanOut])
async def plan_bulk_switch(
    body: BulkSwitchPlanRequest,
    session: AsyncSession = Depends(get_db),
    cf: CloudflareClient = Depends(get_cloudflare_client),
):
    service = SwitchService(session, cf)
    try:
        plans = await service.plan_bulk_switch(
            body.switch_group_id, body.target_datacenter_id, domain_id=body.domain_id
        )
    except SwitchValidationError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    return [SwitchPlanOut.model_validate(p) for p in plans]


@router.post("/switch/bulk/execute", response_model=BulkSwitchExecutionOut)
async def execute_bulk_switch(
    body: BulkSwitchExecuteRequest,
    session: AsyncSession = Depends(get_db),
    cf: CloudflareClient = Depends(get_cloudflare_client),
):
    service = SwitchService(session, cf)
    try:
        summary = await service.execute_bulk_switch(
            body.switch_group_id, body.target_datacenter_id, body.actor, domain_id=body.domain_id
        )
    except SwitchValidationError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    return BulkSwitchExecutionOut(
        switch_group_id=summary.switch_group_id,
        target_datacenter_id=summary.target_datacenter_id,
        succeeded_count=len(summary.succeeded),
        failed_count=len(summary.failed),
        skipped_count=len(summary.skipped),
        results=[_to_execution_out(r) for r in summary.results],
    )


@router.post("/rollback/{audit_log_entry_id}", response_model=SwitchExecutionOut)
async def rollback(
    audit_log_entry_id: int,
    body: RollbackRequest,
    session: AsyncSession = Depends(get_db),
    cf: CloudflareClient = Depends(get_cloudflare_client),
):
    service = SwitchService(session, cf)
    try:
        result = await service.rollback(audit_log_entry_id, body.actor)
    except SwitchValidationError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    return _to_execution_out(result)


@router.get("/audit-log", response_model=AuditLogPageOut)
async def get_audit_log(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    domain_id: int | None = None,
    dns_target_id: int | None = None,
    switch_group_id: int | None = None,
    session: AsyncSession = Depends(get_db),
):
    stmt = select(AuditLogEntry)
    count_stmt = select(func.count()).select_from(AuditLogEntry)

    if dns_target_id is not None:
        stmt = stmt.where(AuditLogEntry.dns_target_id == dns_target_id)
        count_stmt = count_stmt.where(AuditLogEntry.dns_target_id == dns_target_id)
    if switch_group_id is not None:
        stmt = stmt.where(AuditLogEntry.switch_group_id == switch_group_id)
        count_stmt = count_stmt.where(AuditLogEntry.switch_group_id == switch_group_id)
    if domain_id is not None:
        stmt = stmt.join(DnsTarget, DnsTarget.id == AuditLogEntry.dns_target_id).where(
            DnsTarget.domain_id == domain_id
        )
        count_stmt = count_stmt.join(
            DnsTarget, DnsTarget.id == AuditLogEntry.dns_target_id
        ).where(DnsTarget.domain_id == domain_id)

    total = (await session.execute(count_stmt)).scalar_one()
    stmt = stmt.order_by(AuditLogEntry.created_at.desc()).offset((page - 1) * page_size).limit(
        page_size
    )
    items = (await session.execute(stmt)).scalars().all()

    return AuditLogPageOut(items=items, total=total, page=page, page_size=page_size)
