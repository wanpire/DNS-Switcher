from sqlalchemy import String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.session import Base


class Domain(Base):
    __tablename__ = "domains"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    cloudflare_zone_id: Mapped[str] = mapped_column(String(64), nullable=False)

    dns_targets: Mapped[list["DnsTarget"]] = relationship(
        back_populates="domain",
    )
