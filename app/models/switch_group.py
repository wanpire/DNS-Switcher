from sqlalchemy import ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.session import Base


class SwitchGroup(Base):
    __tablename__ = "switch_groups"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(100), unique=True, nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)

    members: Mapped[list["SwitchGroupMember"]] = relationship(
        back_populates="switch_group",
        cascade="all, delete-orphan",
        order_by="SwitchGroupMember.position",
    )


class SwitchGroupMember(Base):
    __tablename__ = "switch_group_members"

    switch_group_id: Mapped[int] = mapped_column(
        ForeignKey("switch_groups.id", ondelete="CASCADE"), primary_key=True
    )
    dns_target_id: Mapped[int] = mapped_column(
        ForeignKey("dns_targets.id", ondelete="CASCADE"), primary_key=True, index=True
    )
    position: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    switch_group: Mapped["SwitchGroup"] = relationship(back_populates="members")
    dns_target: Mapped["DnsTarget"] = relationship()
