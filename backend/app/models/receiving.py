import enum
from datetime import datetime

from sqlalchemy import DateTime, Enum, Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.session import Base


class ReceivingStatus(str, enum.Enum):
    receiving_waiting = "receiving_waiting"
    receiving_started = "receiving_started"
    documents_uploaded = "documents_uploaded"
    ocr_processed = "ocr_processed"
    matched_full = "matched_full"
    matched_partial = "matched_partial"
    has_extra_items = "has_extra_items"
    requires_correction = "requires_correction"
    confirmed_full = "confirmed_full"
    confirmed_partial = "confirmed_partial"
    control_required = "control_required"
    sent_to_accounting = "sent_to_accounting"
    accounting_error = "accounting_error"


class ReceivingItemStatus(str, enum.Enum):
    matched = "matched"
    missing = "missing"
    extra = "extra"
    extra_accepted = "extra_accepted"
    quantity_mismatch = "quantity_mismatch"
    price_mismatch = "price_mismatch"
    replacement_candidate = "replacement_candidate"
    crossed_out = "crossed_out"
    accepted = "accepted"
    rejected = "rejected"
    manual_review = "manual_review"


class Receiving(Base):
    __tablename__ = "receivings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    request_id: Mapped[str] = mapped_column(String(64), index=True)
    order_number: Mapped[str] = mapped_column(String(64), index=True)
    venue: Mapped[str] = mapped_column(String(255))
    supplier: Mapped[str] = mapped_column(String(255))
    delivery_address: Mapped[str | None] = mapped_column(String(500), nullable=True)
    chat_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    user_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    status: Mapped[ReceivingStatus] = mapped_column(
        Enum(ReceivingStatus), default=ReceivingStatus.receiving_started
    )
    comment: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    order_items: Mapped[list["OrderItemSnapshot"]] = relationship(
        cascade="all, delete-orphan", back_populates="receiving"
    )
    documents: Mapped[list["ReceivingDocument"]] = relationship(
        cascade="all, delete-orphan", back_populates="receiving"
    )
    items: Mapped[list["ReceivingItem"]] = relationship(
        cascade="all, delete-orphan", back_populates="receiving"
    )


class OrderItemSnapshot(Base):
    __tablename__ = "order_item_snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    receiving_id: Mapped[int] = mapped_column(ForeignKey("receivings.id"), index=True)
    name: Mapped[str] = mapped_column(String(500))
    quantity: Mapped[float] = mapped_column(Float)
    unit: Mapped[str] = mapped_column(String(32), default="шт")
    price: Mapped[float] = mapped_column(Float, default=0)
    supplier_product_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    role: Mapped[str | None] = mapped_column(String(64), nullable=True)

    receiving: Mapped[Receiving] = relationship(back_populates="order_items")


class ReceivingDocument(Base):
    __tablename__ = "receiving_documents"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    receiving_id: Mapped[int] = mapped_column(ForeignKey("receivings.id"), index=True)
    file_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    file_type: Mapped[str] = mapped_column(String(32), default="photo")
    source: Mapped[str] = mapped_column(String(64), default="max")
    file_url: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    ocr_status: Mapped[str] = mapped_column(String(64), default="uploaded")
    raw_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    recognized_items_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    supplier_legal_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    invoice_number: Mapped[str | None] = mapped_column(String(128), nullable=True)
    invoice_date: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    receiving: Mapped[Receiving] = relationship(back_populates="documents")


class ReceivingItem(Base):
    __tablename__ = "receiving_items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    receiving_id: Mapped[int] = mapped_column(ForeignKey("receivings.id"), index=True)
    item_name_from_order: Mapped[str | None] = mapped_column(String(500), nullable=True)
    item_name_from_invoice: Mapped[str | None] = mapped_column(String(500), nullable=True)
    ordered_quantity: Mapped[float] = mapped_column(Float, default=0)
    received_quantity: Mapped[float] = mapped_column(Float, default=0)
    unit: Mapped[str] = mapped_column(String(32), default="шт")
    ordered_price: Mapped[float] = mapped_column(Float, default=0)
    invoice_price: Mapped[float] = mapped_column(Float, default=0)
    status: Mapped[ReceivingItemStatus] = mapped_column(Enum(ReceivingItemStatus))
    comment: Mapped[str | None] = mapped_column(Text, nullable=True)

    receiving: Mapped[Receiving] = relationship(back_populates="items")
