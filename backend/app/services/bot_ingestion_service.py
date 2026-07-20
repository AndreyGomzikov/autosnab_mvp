from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any
from uuid import uuid4

from sqlalchemy.orm import Session

from app.models.ingestion import IngestionUpload
from app.models.receiving import Receiving

SUPPORTED_EXTENSIONS_NOW = {".jpg", ".jpeg", ".png", ".pdf"}
SUPPORTED_EXTENSIONS_LATER = {".xml", ".xls", ".xlsx"}
SUPPORTED_IMAGE_TYPES = {"image/jpeg", "image/png", "application/pdf"}
SUPPORTED_DOCUMENT_KINDS_NOW = {"primary_document"}

DRAFT_STATUS = "collecting"


def create_upload_journal(
    db: Session,
    *,
    source_channel: str,
    document_kind: str,
    user_id: str | None,
    username: str | None,
    chat_id: str | None,
    organization_name: str | None,
    point_name: str | None,
    original_filename: str,
    file_type: str,
    raw_file_path: str,
    files_count: int,
    trace_id: str | None,
    status: str,
    error_text: str | None = None,
) -> IngestionUpload:
    upload = IngestionUpload(
        upload_id=build_upload_id(),
        trace_id=trace_id,
        source_channel=source_channel,
        document_kind=document_kind,
        user_id=user_id,
        username=username,
        chat_id=chat_id,
        organization_name=organization_name,
        point_name=point_name,
        original_filename=original_filename,
        file_type=file_type,
        raw_file_path=raw_file_path,
        files_count=files_count,
        status=status,
        error_text=error_text,
    )
    db.add(upload)
    db.commit()
    db.refresh(upload)
    return upload


def update_upload_journal(
    db: Session,
    upload_id: str,
    *,
    status: str | None = None,
    error_text: str | None = None,
    trace_id: str | None = None,
    review_id: int | None = None,
) -> IngestionUpload:
    upload = get_upload_journal(db, upload_id)
    if status is not None:
        upload.status = status
    if trace_id is not None:
        upload.trace_id = trace_id
    upload.error_text = error_text
    if review_id is not None:
        upload.review_id = review_id
    db.add(upload)
    db.commit()
    db.refresh(upload)
    return upload


def get_upload_journal(db: Session, upload_id: str) -> IngestionUpload:
    upload = db.query(IngestionUpload).filter(IngestionUpload.upload_id == upload_id).first()
    if upload is None:
        raise ValueError("Загрузка бота не найдена")
    return upload


def build_upload_id() -> str:
    return f"bot-upload-{uuid4().hex[:12]}"


def get_active_draft(db: Session, chat_id: str) -> IngestionUpload | None:
    return (
        db.query(IngestionUpload)
        .filter(IngestionUpload.chat_id == chat_id, IngestionUpload.status == DRAFT_STATUS)
        .order_by(IngestionUpload.created_at.desc())
        .first()
    )


def list_draft_files(upload: IngestionUpload) -> list[Path]:
    draft_dir = Path(upload.raw_file_path)
    if not draft_dir.is_dir():
        return []
    return sorted(path for path in draft_dir.iterdir() if path.is_file())


def draft_display_name(path: Path) -> str:
    """Recover the original filename from a `NNN-<name>` draft page path."""
    return path.name.split("-", 1)[1] if "-" in path.name else path.name


def append_draft_file(db: Session, upload: IngestionUpload, *, filename: str) -> IngestionUpload:
    upload.files_count += 1
    upload.original_filename = filename if upload.files_count == 1 else upload.original_filename
    upload.file_type = "multipage" if upload.files_count > 1 else upload.file_type
    db.add(upload)
    db.commit()
    db.refresh(upload)
    return upload


def delete_draft(db: Session, upload: IngestionUpload) -> None:
    draft_dir = Path(upload.raw_file_path)
    if draft_dir.is_dir():
        shutil.rmtree(draft_dir, ignore_errors=True)
    db.delete(upload)
    db.commit()


def get_latest_upload_for_chat(db: Session, chat_id: str) -> IngestionUpload | None:
    return (
        db.query(IngestionUpload)
        .filter(IngestionUpload.chat_id == chat_id, IngestionUpload.status != DRAFT_STATUS)
        .order_by(IngestionUpload.created_at.desc())
        .first()
    )


def classify_bot_file(
    filename: str | None,
    content_type: str | None,
    *,
    document_kind: str,
) -> tuple[bool, str | None, str]:
    safe_name = Path(filename or "document").name
    extension = Path(safe_name).suffix.lower()
    normalized_type = content_type or "application/octet-stream"
    if document_kind not in SUPPORTED_DOCUMENT_KINDS_NOW:
        return False, "Сценарий QR-чека или специального типа документа пока не поддерживается в backend.", extension
    if extension in SUPPORTED_EXTENSIONS_NOW or normalized_type in SUPPORTED_IMAGE_TYPES:
        return True, None, extension
    if extension in SUPPORTED_EXTENSIONS_LATER:
        return False, "Формат файла входит в ТЗ бота, но его backend-разбор еще не реализован.", extension
    return False, "Формат файла пока не поддерживается. Загрузите JPG, PNG или PDF.", extension


def derive_bot_result(
    response: dict[str, Any] | None,
    receiving: Receiving | None = None,
) -> tuple[str, str, bool, str | None]:
    if not response:
        return "processing_error", "Обработка завершилась без результата.", False, None
    review_status = response.get("status")
    duplicate = _response_duplicate_flag(response, receiving=receiving)
    if duplicate:
        return "possible_duplicate", "Документ похож на уже загруженный и требует проверки на дубль.", True, review_status
    if review_status == "needs_review":
        return "requires_review", "Документ обработан, но требует проверки в модуле проверки данных.", False, review_status
    if review_status == "ready":
        return "transferred_to_review", "Документ обработан и передан в модуль проверки данных.", False, review_status
    if response.get("google_spreadsheet_error"):
        return "processed", "Документ распознан, но при публикации результата возникла внешняя ошибка.", False, review_status
    return "processed", "Документ обработан.", False, review_status


def build_bot_next_actions(response: dict[str, Any] | None) -> dict[str, Any]:
    if not response:
        return {}
    next_actions = dict(response.get("next_actions") or {})
    review_id = response.get("review_id")
    result_code, _, _, _ = derive_bot_result(response)
    return {
        "review_id": review_id,
        "review_status": response.get("status"),
        "result_code": result_code,
        "review_links": next_actions,
    }


def build_bot_document_summary(receiving: Receiving | None) -> dict[str, Any] | None:
    if receiving is None:
        return None

    document = receiving.documents[-1] if receiving.documents else None
    if document and document.recognized_items_json:
        try:
            stored = json.loads(document.recognized_items_json)
        except json.JSONDecodeError:
            stored = {}
    else:
        stored = {}

    header = stored.get("header") or {}
    items = stored.get("items")
    source_files = (header.get("parser_metadata") or {}).get("source_files") or []
    supplier = document.supplier_legal_name if document else None
    invoice_number = document.invoice_number if document else receiving.order_number
    invoice_date = document.invoice_date if document else None
    total_sum = header.get("total_sum")

    if total_sum in ("", None):
        total_sum = None
    elif isinstance(total_sum, str):
        try:
            total_sum = float(total_sum)
        except ValueError:
            total_sum = None

    return {
        "supplier": supplier or receiving.supplier or None,
        "invoice_number": invoice_number or None,
        "invoice_date": invoice_date or None,
        "document_form": header.get("document_form") or None,
        "total_sum": total_sum,
        "items_count": len(items) if isinstance(items, list) and items else len(receiving.items),
        "pages_count": len(source_files) if isinstance(source_files, list) else 0,
        "duplicate_indicator": header.get("duplicate_indicator") or None,
    }


def build_source_metadata(
    upload: IngestionUpload,
) -> dict[str, Any]:
    return {
        "source_channel": upload.source_channel,
        "document_kind": upload.document_kind,
        "upload_id": upload.upload_id,
        "source_user_id": upload.user_id,
        "source_username": upload.username,
        "source_chat_id": upload.chat_id,
        "organization_name": upload.organization_name,
        "point_name": upload.point_name,
    }


def _response_duplicate_flag(response: dict[str, Any], *, receiving: Receiving | None) -> bool:
    if receiving is not None:
        document = receiving.documents[-1] if receiving.documents else None
        if document and document.recognized_items_json:
            try:
                stored = json.loads(document.recognized_items_json)
            except json.JSONDecodeError:
                stored = {}
            header = stored.get("header") or {}
            if str(header.get("duplicate_indicator") or "").strip() in {"Да", "?"}:
                return True
    for issue in response.get("issues") or []:
        if "дуб" in str(issue).casefold():
            return True
    return False
