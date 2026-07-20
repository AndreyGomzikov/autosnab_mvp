from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.session import Base


class IngestionUpload(Base):
    __tablename__ = "ingestion_uploads"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    upload_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    trace_id: Mapped[str | None] = mapped_column(String(128), index=True, nullable=True)
    source_channel: Mapped[str] = mapped_column(String(64), default="telegram_bot")
    document_kind: Mapped[str] = mapped_column(String(64), default="primary_document")
    user_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    username: Mapped[str | None] = mapped_column(String(255), nullable=True)
    chat_id: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    organization_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    point_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    original_filename: Mapped[str] = mapped_column(String(255))
    file_type: Mapped[str] = mapped_column(String(64))
    raw_file_path: Mapped[str] = mapped_column(String(1000))
    files_count: Mapped[int] = mapped_column(Integer, default=1)
    status: Mapped[str] = mapped_column(String(64), default="file_received", index=True)
    error_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    review_id: Mapped[int | None] = mapped_column(ForeignKey("receivings.id"), nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
