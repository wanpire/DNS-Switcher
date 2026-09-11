from sqlalchemy import ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.session import Base


class TargetDatacenterIp(Base):
    """The desired IP content for one (DnsTarget, Datacenter, slot_index).

    A DnsTarget can have 1..N of these per Datacenter -- more than one for
    a load-balanced target with several simultaneous A/AAAA records.
    slot_index orders them within a (target, datacenter) pair; the same
    slot_index across datacenters corresponds to "the same physical
    record" for switch purposes -- see SwitchService, which reuses a
    Cloudflare record's id across a switch (PATCHing its content) when the
    source and destination datacenter have matching slot counts, rather
    than always creating/deleting records.

    cloudflare_record_id is populated only when this row is the one
    currently live in Cloudflare (i.e. its datacenter matches the target's
    current_datacenter_id) -- null otherwise, since a non-current
    datacenter's candidate IP has no corresponding live record.
    """

    __tablename__ = "target_datacenter_ips"
    __table_args__ = (
        UniqueConstraint(
            "dns_target_id", "datacenter_id", "slot_index", name="uq_target_dc_ip_slot"
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    dns_target_id: Mapped[int] = mapped_column(
        ForeignKey("dns_targets.id", ondelete="CASCADE"), nullable=False, index=True
    )
    datacenter_id: Mapped[int] = mapped_column(
        ForeignKey("datacenters.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    slot_index: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    ip_address: Mapped[str] = mapped_column(String(45), nullable=False)
    cloudflare_record_id: Mapped[str | None] = mapped_column(String(64), nullable=True)

    dns_target: Mapped["DnsTarget"] = relationship(back_populates="datacenter_ips")
    datacenter: Mapped["Datacenter"] = relationship()
