import enum

from sqlalchemy import Boolean, Enum as SAEnum, ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.session import Base


class RecordType(str, enum.Enum):
    A = "A"
    AAAA = "AAAA"
    CNAME = "CNAME"


class DnsTarget(Base):
    __tablename__ = "dns_targets"
    __table_args__ = (
        UniqueConstraint(
            "domain_id", "name", "record_type", name="uq_dns_targets_domain_name_type"
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    domain_id: Mapped[int] = mapped_column(
        ForeignKey("domains.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    record_type: Mapped[RecordType] = mapped_column(
        SAEnum(RecordType, name="dns_record_type"), nullable=False
    )
    cloudflare_record_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    current_datacenter_id: Mapped[int | None] = mapped_column(
        ForeignKey("datacenters.id", ondelete="RESTRICT"), nullable=True, index=True
    )
    proxied: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    domain: Mapped["Domain"] = relationship(back_populates="dns_targets")
    current_datacenter: Mapped["Datacenter | None"] = relationship(
        back_populates="dns_targets"
    )
