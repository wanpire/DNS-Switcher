import enum

from sqlalchemy import Enum as SAEnum
from sqlalchemy import String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.session import Base


class DatacenterStatus(str, enum.Enum):
    active = "active"
    standby = "standby"
    disabled = "disabled"


class Datacenter(Base):
    __tablename__ = "datacenters"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(100), unique=True, nullable=False)
    status: Mapped[DatacenterStatus] = mapped_column(
        SAEnum(DatacenterStatus, name="datacenter_status"),
        nullable=False,
        default=DatacenterStatus.active,
        server_default=DatacenterStatus.active.value,
    )
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    ip_address: Mapped[str | None] = mapped_column(String(45), nullable=True)

    dns_targets: Mapped[list["DnsTarget"]] = relationship(
        back_populates="current_datacenter"
    )
