from datetime import datetime

from sqlalchemy import DateTime, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.session import Base


class AccountingMapping(Base):
    __tablename__ = "accounting_mappings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    venue: Mapped[str] = mapped_column(String(255), index=True)
    supplier_product_name: Mapped[str] = mapped_column(String(500), index=True)
    normalized_product_name: Mapped[str] = mapped_column(String(500), index=True)
    accounting_product_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    accounting_product_name: Mapped[str | None] = mapped_column(String(500), nullable=True)
    unit: Mapped[str | None] = mapped_column(String(32), nullable=True)
    status: Mapped[str] = mapped_column(String(64), default="manual_confirmed")
    comment: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class AccountingExport(Base):
    __tablename__ = "accounting_exports"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    receiving_id: Mapped[int] = mapped_column(Integer, index=True)
    request_id: Mapped[str] = mapped_column(String(64), index=True)
    order_number: Mapped[str] = mapped_column(String(64), index=True)
    target_system: Mapped[str] = mapped_column(String(64), default="iiko")
    status: Mapped[str] = mapped_column(String(64), default="prepared")
    payload_json: Mapped[str] = mapped_column(Text)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
