from datetime import datetime

from pydantic import BaseModel, ConfigDict

from app.models import ActionType, AuditStatus, DatacenterStatus, RecordType


class DatacenterOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    status: DatacenterStatus
    notes: str | None


class DomainOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    cloudflare_zone_id: str


class DnsTargetOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    domain_id: int
    name: str
    record_type: RecordType
    current_datacenter_id: int | None
    proxied: bool


class SwitchGroupOut(BaseModel):
    id: int
    name: str
    description: str | None
    member_dns_target_ids: list[int]


class SlotDiffOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    slot_index: int
    current_ip: str | None
    new_ip: str | None


class SwitchPlanOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    dns_target_id: int
    fqdn: str
    record_type: str
    current_datacenter_id: int | None
    target_datacenter_id: int
    slot_diffs: list[SlotDiffOut]
    slot_count_mismatch: bool
    no_op: bool
    unavailable: bool
    unavailable_reason: str | None


class SingleSwitchPlanRequest(BaseModel):
    dns_target_id: int
    target_datacenter_id: int


class SingleSwitchExecuteRequest(BaseModel):
    dns_target_id: int
    target_datacenter_id: int
    actor: str
    allow_slot_count_mismatch: bool = False


class BulkSwitchPlanRequest(BaseModel):
    switch_group_id: int
    target_datacenter_id: int
    domain_id: int | None = None


class BulkSwitchExecuteRequest(BaseModel):
    switch_group_id: int
    target_datacenter_id: int
    actor: str
    domain_id: int | None = None


class RollbackRequest(BaseModel):
    actor: str


class SlotResultOut(BaseModel):
    slot_index: int
    ip_address: str | None
    cloudflare_record_id: str | None
    action: str | None
    success: bool
    error: str | None


class SwitchExecutionOut(BaseModel):
    dns_target_id: int
    success: bool
    skipped: bool
    error_message: str | None
    audit_log_entry_id: int | None
    slot_results: list[SlotResultOut]
    unavailable: bool


class BulkSwitchExecutionOut(BaseModel):
    switch_group_id: int
    target_datacenter_id: int
    succeeded_count: int
    failed_count: int
    skipped_count: int
    results: list[SwitchExecutionOut]


class AuditLogEntryOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    created_at: datetime
    actor: str
    action_type: ActionType
    dns_target_id: int | None
    switch_group_id: int | None
    previous_datacenter_id: int | None
    new_datacenter_id: int
    slot_results: list[dict] | None
    status: AuditStatus
    error_message: str | None
    rollback_of_id: int | None


class AuditLogPageOut(BaseModel):
    items: list[AuditLogEntryOut]
    total: int
    page: int
    page_size: int
