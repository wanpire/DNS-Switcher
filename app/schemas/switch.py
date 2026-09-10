from datetime import datetime

from pydantic import BaseModel, ConfigDict

from app.models import ActionType, AuditStatus, DatacenterStatus, RecordType


class DatacenterOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    status: DatacenterStatus
    ip_address: str | None
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
    cloudflare_record_id: str | None
    current_datacenter_id: int | None
    proxied: bool


class SwitchGroupOut(BaseModel):
    id: int
    name: str
    description: str | None
    member_dns_target_ids: list[int]


class SwitchPlanOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    dns_target_id: int
    fqdn: str
    record_type: str
    current_datacenter_id: int | None
    current_content: str | None
    target_datacenter_id: int
    new_content: str
    no_op: bool


class SingleSwitchPlanRequest(BaseModel):
    dns_target_id: int
    target_datacenter_id: int


class SingleSwitchExecuteRequest(BaseModel):
    dns_target_id: int
    target_datacenter_id: int
    actor: str


class BulkSwitchPlanRequest(BaseModel):
    switch_group_id: int
    target_datacenter_id: int


class BulkSwitchExecuteRequest(BaseModel):
    switch_group_id: int
    target_datacenter_id: int
    actor: str


class RollbackRequest(BaseModel):
    actor: str


class SwitchExecutionOut(BaseModel):
    dns_target_id: int
    success: bool
    skipped: bool
    error_message: str | None
    audit_log_entry_id: int | None


class BulkSwitchExecutionOut(BaseModel):
    switch_group_id: int
    target_datacenter_id: int
    succeeded_count: int
    failed_count: int
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
    cloudflare_record_id: str
    status: AuditStatus
    error_message: str | None
    rollback_of_id: int | None


class AuditLogPageOut(BaseModel):
    items: list[AuditLogEntryOut]
    total: int
    page: int
    page_size: int
