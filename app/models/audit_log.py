import enum
from datetime import datetime

from sqlalchemy import DateTime, Enum as SAEnum, ForeignKey, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.session import Base


class ActionType(str, enum.Enum):
    single = "single"
    bulk = "bulk"
    rollback = "rollback"


class AuditStatus(str, enum.Enum):
    success = "success"
    failed = "failed"
    rolled_back = "rolled_back"


class AuditLogEntry(Base):
    __tablename__ = "audit_log_entries"

    id: Mapped[int] = mapped_column(primary_key=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), index=True
    )
    actor: Mapped[str] = mapped_column(String(255), nullable=False)
    action_type: Mapped[ActionType] = mapped_column(
        SAEnum(ActionType, name="audit_action_type"), nullable=False
    )
    dns_target_id: Mapped[int | None] = mapped_column(
        ForeignKey("dns_targets.id", ondelete="SET NULL"), nullable=True, index=True
    )
    switch_group_id: Mapped[int | None] = mapped_column(
        ForeignKey("switch_groups.id", ondelete="SET NULL"), nullable=True, index=True
    )
    previous_datacenter_id: Mapped[int] = mapped_column(
        ForeignKey("datacenters.id", ondelete="RESTRICT"), nullable=False
    )
    new_datacenter_id: Mapped[int] = mapped_column(
        ForeignKey("datacenters.id", ondelete="RESTRICT"), nullable=False
    )
    cloudflare_record_id: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[AuditStatus] = mapped_column(
        SAEnum(AuditStatus, name="audit_status"), nullable=False
    )
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    dns_target: Mapped["DnsTarget | None"] = relationship()
    switch_group: Mapped["SwitchGroup | None"] = relationship()
    previous_datacenter: Mapped["Datacenter"] = relationship(foreign_keys=[previous_datacenter_id])
    new_datacenter: Mapped["Datacenter"] = relationship(foreign_keys=[new_datacenter_id])
