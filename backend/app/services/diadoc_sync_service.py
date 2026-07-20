from __future__ import annotations

import base64
import json
import mimetypes
import re
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from sqlalchemy import or_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, object_session

from app.config import settings
from app.models.diadoc import (
    DiadocArtifact,
    DiadocDelivery,
    DiadocDocument,
    DiadocLease,
    DiadocSyncState,
)
from app.models.receiving import Receiving, ReceivingDocument
from app.schemas.invoice_review import InvoiceReviewCreateRequest, RecognizedInvoiceItem
from app.schemas.receiving import CompareInvoiceRequest, InvoiceItemIn
from app.services.diadoc_client import (
    DiadocApiError,
    DiadocBinaryResponse,
    DiadocClient,
    DiadocContentTooLargeError,
)
from app.services.diadoc_oauth_service import get_diadoc_oauth_status
from app.services.diadoc_xml_parser_service import parse_diadoc_invoice_xml
from app.services.document_extraction_service import extract_invoice_document
from app.services.google_oauth_service import get_oauth_status as get_google_oauth_status
from app.services.invoice_review_service import (
    create_invoice_review,
    create_real_google_sheet_for_review,
    update_invoice_review,
)
from app.services.receiving_service import compare_invoice

SUPPORTED_TYPE_MARKERS = (
    "UniversalTransferDocument",
    "UniversalCorrectionDocument",
    "XmlTorg12",
    "Torg12",
    "Invoice",
    "AcceptanceCertificate",
    "УПД",
)
SERVICE_ATTACHMENT_MARKERS = (
    "Confirmation",
    "Receipt",
    "Signature",
    "Revocation",
    "Resolution",
    "AmendmentRequest",
    "Rejection",
)
UNSTRUCTURED_EXTENSIONS = {
    ".pdf",
    ".jpg",
    ".jpeg",
    ".png",
    ".webp",
    ".tif",
    ".tiff",
}
RETRY_STATUSES = {"failed", "content_unavailable"}
MANUAL_RETRY_STATUSES = RETRY_STATUSES | {"dead_letter"}
DELIVERY_RETRY_STATUSES = {"pending", "failed"}
DELIVERY_TYPES = {"google_sheets", "print_form"}
_SYNC_LOCK = threading.Lock()
_SYNC_OWNER_ID = uuid4().hex
_SYNC_LEASE_NAME = "diadoc_sync"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class PermanentDiadocDocumentError(ValueError):
    pass


def get_diadoc_status(db: Session) -> dict[str, Any]:
    state = None
    if settings.diadoc_box_id:
        state = db.query(DiadocSyncState).filter(DiadocSyncState.box_id == settings.diadoc_box_id).first()
    failed_count = db.query(DiadocDocument).filter(DiadocDocument.status.in_(MANUAL_RETRY_STATUSES)).count()
    delivery_queue_size = (
        db.query(DiadocDelivery)
        .filter(DiadocDelivery.status.in_(DELIVERY_RETRY_STATUSES | {"in_progress", "dead_letter"}))
        .count()
    )
    dead_letter_count = db.query(DiadocDocument).filter(DiadocDocument.status == "dead_letter").count()
    delivery_dead_letter_count = (
        db.query(DiadocDelivery).filter(DiadocDelivery.status == "dead_letter").count()
    )
    oauth = get_diadoc_oauth_status()
    auth_configured = bool(
        settings.diadoc_access_token
        or (
            settings.diadoc_refresh_token
            and settings.diadoc_client_id
            and settings.diadoc_client_secret
        )
    )
    return {
        "enabled": settings.diadoc_integration_enabled,
        "configured": bool(settings.diadoc_box_id and auth_configured),
        "box_id": settings.diadoc_box_id,
        "last_sync_at": state.last_sync_at.isoformat() if state and state.last_sync_at else None,
        "after_index_key_present": bool(state and state.after_index_key),
        "last_error": state.last_error if state else None,
        "scheduler_enabled": settings.diadoc_scheduler_enabled,
        "sync_interval_seconds": settings.diadoc_sync_interval_seconds,
        "initial_sync_mode": settings.diadoc_initial_sync_mode,
        "max_pages_per_sync": settings.diadoc_max_pages_per_sync,
        "retry_queue_size": failed_count,
        "delivery_queue_size": delivery_queue_size,
        "dead_letter_count": dead_letter_count,
        "delivery_dead_letter_count": delivery_dead_letter_count,
        "oauth": oauth,
    }

def list_diadoc_organizations() -> list[dict[str, Any]]:
    payload = DiadocClient().get_my_organizations()
    organizations = payload.get("Organizations") or payload.get("organizations") or []
    result = []
    for item in organizations:
        boxes = item.get("Boxes") or item.get("boxes") or []
        result.append(
            {
                "organization_id": item.get("OrgId") or item.get("OrganizationId") or item.get("orgId"),
                "name": item.get("FullName") or item.get("ShortName") or item.get("Name") or item.get("name"),
                "inn": item.get("Inn") or item.get("inn"),
                "kpp": item.get("Kpp") or item.get("kpp"),
                "box_ids": [
                    box.get("BoxId") or box.get("boxId")
                    for box in boxes
                    if box.get("BoxId") or box.get("boxId")
                ],
            }
        )
    return result



def run_diadoc_preflight() -> dict[str, Any]:
    checks: list[dict[str, Any]] = []

    def add(
        name: str,
        ready: bool,
        detail: str,
        *,
        required: bool = True,
    ) -> None:
        checks.append(
            {
                "name": name,
                "ready": ready,
                "required": required,
                "detail": detail,
            }
        )

    add(
        "integration_enabled",
        settings.diadoc_integration_enabled,
        "Интеграция включена"
        if settings.diadoc_integration_enabled
        else "Установите DIADOC_INTEGRATION_ENABLED=true",
    )
    add(
        "box_id",
        bool(settings.diadoc_box_id),
        "BoxId задан"
        if settings.diadoc_box_id
        else "Заполните DIADOC_BOX_ID",
    )
    oauth = get_diadoc_oauth_status()
    add(
        "oauth",
        bool(oauth.get("authorized")),
        "OAuth-токены доступны"
        if oauth.get("authorized")
        else "Пройдите OAuth-авторизацию Диадок",
    )

    storage_ready, storage_detail = _check_document_storage()
    add("document_storage", storage_ready, storage_detail)

    if settings.google_sheets_enabled:
        sheet_configured = bool(
            settings.google_target_spreadsheet_id
            and settings.google_target_sheet_name
        )
        add(
            "google_sheets",
            sheet_configured,
            "Целевая Google-таблица настроена"
            if sheet_configured
            else (
                "Заполните GOOGLE_TARGET_SPREADSHEET_ID и "
                "GOOGLE_TARGET_SHEET_NAME"
            ),
        )
        google_oauth = get_google_oauth_status()
        add(
            "google_oauth",
            bool(google_oauth.get("authorized")),
            "Google OAuth готов"
            if google_oauth.get("authorized")
            else (
                "Google OAuth недоступен: "
                f"{google_oauth.get('error') or 'пройдите авторизацию'}"
            ),
        )
    else:
        add(
            "google_sheets",
            True,
            "Google Sheets отключён настройкой",
            required=False,
        )

    box_payload: dict[str, Any] | None = None
    document_types_count = 0
    if (
        settings.diadoc_integration_enabled
        and settings.diadoc_box_id
        and oauth.get("authorized")
    ):
        client = DiadocClient()
        try:
            box_payload = client.get_box(
                box_id=settings.diadoc_box_id,
            )
            returned_box_id = (
                box_payload.get("BoxIdGuid")
                or box_payload.get("boxIdGuid")
                or box_payload.get("BoxId")
                or box_payload.get("boxId")
            )
            matches = _box_ids_match(
                settings.diadoc_box_id,
                str(returned_box_id or ""),
            )
            add(
                "box_access",
                matches,
                "Ящик доступен текущему пользователю"
                if matches
                else "API вернул другой ящик",
            )
        except Exception as exc:  # noqa: BLE001 - preflight reports diagnostics
            add(
                "box_access",
                False,
                f"Не удалось открыть ящик: {exc}",
            )

        try:
            payload = client.get_document_types(
                box_id=settings.diadoc_box_id,
            )
            document_types = (
                payload.get("DocumentTypes")
                or payload.get("documentTypes")
                or []
            )
            document_types_count = len(document_types)
            add(
                "document_types",
                document_types_count > 0,
                f"Доступно типов документов: {document_types_count}",
                required=False,
            )
        except Exception as exc:  # noqa: BLE001 - optional diagnostic
            add(
                "document_types",
                False,
                f"Не удалось получить типы документов: {exc}",
                required=False,
            )

    required_ready = all(
        item["ready"]
        for item in checks
        if item["required"]
    )
    return {
        "ready": required_ready,
        "checks": checks,
        "box": {
            "box_id_guid": (
                box_payload.get("BoxIdGuid")
                if box_payload
                else None
            ),
            "title": (
                box_payload.get("Title")
                if box_payload
                else None
            ),
        },
        "document_types_count": document_types_count,
        "initial_sync_mode": settings.diadoc_initial_sync_mode,
        "max_pages_per_sync": settings.diadoc_max_pages_per_sync,
    }


def _check_document_storage() -> tuple[bool, str]:
    root = Path(settings.diadoc_documents_dir)
    probe = root / ".diadoc-write-test"
    try:
        root.mkdir(parents=True, exist_ok=True)
        probe.write_text("ok", encoding="utf-8")
        probe.unlink(missing_ok=True)
        return True, f"Каталог доступен для записи: {root}"
    except OSError as exc:
        return False, f"Каталог недоступен для записи: {exc}"


def _box_ids_match(expected: str, actual: str) -> bool:
    normalize = lambda value: re.sub(
        r"[^0-9a-f]",
        "",
        value.casefold().split("@", 1)[0],
    )
    return bool(expected and actual) and normalize(expected) == normalize(actual)

def sync_diadoc_documents(db: Session, *, create_google_sheet: bool = True) -> dict[str, Any]:
    _validate_configuration()
    if not _SYNC_LOCK.acquire(blocking=False):
        return _empty_result(status="busy")
    lease_acquired = False
    try:
        lease_acquired = _acquire_sync_lease(db)
        if not lease_acquired:
            return _empty_result(status="busy")
        return _sync_locked(db, create_google_sheet=create_google_sheet)
    finally:
        if lease_acquired:
            _release_sync_lease(db)
        _SYNC_LOCK.release()


def retry_failed_diadoc_documents(
    db: Session,
    *,
    client: DiadocClient | None = None,
    create_google_sheet: bool = True,
    include_dead_letter: bool = False,
    force: bool = False,
    document_id: int | None = None,
) -> dict[str, int]:
    client = client or DiadocClient()
    counters = {"retried": 0, "recovered": 0, "failed": 0}
    statuses = MANUAL_RETRY_STATUSES if include_dead_letter else RETRY_STATUSES
    query = db.query(DiadocDocument).filter(DiadocDocument.status.in_(statuses))
    if document_id is not None:
        query = query.filter(DiadocDocument.id == document_id)
    candidates = (
        query.order_by(DiadocDocument.updated_at.asc())
        .limit(max(1, settings.diadoc_retry_batch_size))
        .all()
    )
    now = _utcnow()
    for document in candidates:
        metadata = _metadata(document)
        if not force and not _retry_due(metadata, now):
            continue
        if force and document.status == "dead_letter":
            _reset_document_retry(db, document)
        counters["retried"] += 1
        try:
            message = client.get_message(box_id=document.box_id, message_id=document.message_id)
            entity = _find_entity(message, document.entity_id)
            if entity is None:
                raise DiadocApiError("Сущность документа не найдена при повторной обработке")
            _process_document(
                db,
                client,
                document,
                entity,
                create_google_sheet=create_google_sheet,
            )
            counters["recovered"] += 1
        except Exception as exc:  # noqa: BLE001 - retry queue must persist diagnostics
            _record_failure(db, document, exc)
            counters["failed"] += 1
    return counters


def retry_diadoc_deliveries(
    db: Session,
    *,
    client: DiadocClient | None = None,
    include_dead_letter: bool = False,
    force: bool = False,
    document_id: int | None = None,
) -> dict[str, int]:
    counters = {"retried": 0, "recovered": 0, "failed": 0, "artifacts_downloaded": 0}
    statuses = set(DELIVERY_RETRY_STATUSES) | {"in_progress"}
    if include_dead_letter:
        statuses.add("dead_letter")
    query = db.query(DiadocDelivery).filter(DiadocDelivery.status.in_(statuses))
    if document_id is not None:
        query = query.filter(DiadocDelivery.diadoc_document_id == document_id)
    deliveries = (
        query.order_by(DiadocDelivery.updated_at.asc())
        .limit(max(1, settings.diadoc_retry_batch_size))
        .all()
    )
    now = _utcnow()
    stale_before = now - timedelta(seconds=max(30, settings.diadoc_delivery_stale_seconds))
    for delivery in deliveries:
        if delivery.status == "in_progress" and not force and delivery.updated_at > stale_before:
            continue
        if not force and not _delivery_due(delivery, now):
            continue
        if force and delivery.status == "dead_letter":
            delivery.status = "pending"
            delivery.attempts = 0
            delivery.next_retry_at = None
            delivery.last_error = None
            db.add(delivery)
            db.commit()
        counters["retried"] += 1
        result = _execute_delivery(db, client, delivery)
        counters["artifacts_downloaded"] += result["artifacts_downloaded"]
        if result["success"]:
            counters["recovered"] += 1
        else:
            counters["failed"] += 1
    return counters

def _sync_locked(
    db: Session,
    *,
    create_google_sheet: bool,
) -> dict[str, Any]:
    state = _get_or_create_state(
        db,
        settings.diadoc_box_id or "",
    )
    client = DiadocClient()
    result = _empty_result()
    try:
        _reconcile_missing_deliveries(
            db,
            create_google_sheet=create_google_sheet,
        )
        delivery_result = retry_diadoc_deliveries(
            db,
            client=client,
        )
        result["deliveries_retried"] = delivery_result["retried"]
        result["deliveries_recovered"] = delivery_result["recovered"]
        result["deliveries_failed"] = delivery_result["failed"]
        result["artifacts_downloaded"] += (
            delivery_result["artifacts_downloaded"]
        )

        retry_result = retry_failed_diadoc_documents(
            db,
            client=client,
            create_google_sheet=create_google_sheet,
        )
        result["documents_retried"] = retry_result["retried"]
        result["documents_recovered"] = retry_result["recovered"]
        result["documents_failed"] += retry_result["failed"]

        _initialize_event_cursor(db, client, state)
        cursor = state.after_index_key
        page_limit = max(1, min(settings.diadoc_sync_limit, 500))
        max_pages = max(1, settings.diadoc_max_pages_per_sync)

        for _page_number in range(max_pages):
            previous_cursor = cursor
            payload = client.get_new_events(
                box_id=settings.diadoc_box_id or "",
                after_index_key=previous_cursor,
                limit=page_limit,
            )
            events = payload.get("Events") or payload.get("events") or []
            result["pages_received"] += 1
            result["events_received"] += len(events)

            for event in events:
                _process_event(
                    db,
                    client,
                    event,
                    result,
                    create_google_sheet=create_google_sheet,
                )

            next_key = (
                _last_index_key(events)
                or payload.get("LastIndexKey")
                or payload.get("lastIndexKey")
            )
            if next_key and str(next_key) != str(previous_cursor or ""):
                cursor = str(next_key)
                state.after_index_key = cursor
                result["next_index_key_present"] = True
                state.last_sync_at = _utcnow()
                state.last_error = None
                db.add(state)
                db.commit()
                _renew_sync_lease(db)

            if not events or len(events) < page_limit:
                break
            if not next_key or str(next_key) == str(previous_cursor or ""):
                break

        state.last_sync_at = _utcnow()
        state.last_error = None
        db.add(state)
        db.commit()
    except Exception as exc:
        db.rollback()
        state = _get_or_create_state(
            db,
            settings.diadoc_box_id or "",
        )
        state.last_sync_at = _utcnow()
        state.last_error = str(exc)
        db.add(state)
        db.commit()
        raise
    return result



def _initialize_event_cursor(
    db: Session,
    client: DiadocClient,
    state: DiadocSyncState,
) -> None:
    mode = (settings.diadoc_initial_sync_mode or "latest").strip().casefold()
    if state.last_sync_at is not None or state.after_index_key:
        return
    if mode not in {"latest", "oldest"}:
        raise ValueError(
            "DIADOC_INITIAL_SYNC_MODE должен быть latest или oldest"
        )
    if mode == "latest":
        last_event = client.get_last_event(
            box_id=settings.diadoc_box_id or "",
        )
        if last_event:
            index_key = (
                last_event.get("IndexKey")
                or last_event.get("indexKey")
            )
            if index_key:
                state.after_index_key = str(index_key)
    state.last_sync_at = _utcnow()
    state.last_error = None
    db.add(state)
    db.commit()


def _reconcile_missing_deliveries(
    db: Session,
    *,
    create_google_sheet: bool,
) -> None:
    documents = (
        db.query(DiadocDocument)
        .filter(
            DiadocDocument.status == "processed",
            DiadocDocument.review_id.is_not(None),
        )
        .order_by(DiadocDocument.id.asc())
        .limit(max(1, settings.diadoc_retry_batch_size * 4))
        .all()
    )
    for document in documents:
        if create_google_sheet and settings.google_sheets_enabled:
            _ensure_delivery(db, document, "google_sheets")
        if (
            settings.diadoc_generate_print_form
            and _document_is_formalized_xml(document)
        ):
            _ensure_delivery(db, document, "print_form")


def _document_is_formalized_xml(
    document: DiadocDocument,
) -> bool:
    metadata = _metadata(document)
    path = Path(document.content_path or "")
    return (
        path.suffix.casefold() == ".xml"
        and _is_formalized_business_entity(metadata)
    )

def _process_event(
    db: Session,
    client: DiadocClient,
    event: dict[str, Any],
    result: dict[str, Any],
    *,
    create_google_sheet: bool,
) -> None:
    message_id = _find_first_value(event, "MessageId", "messageId")
    if not message_id:
        result["documents_skipped"] += 1
        return
    message = client.get_message(box_id=settings.diadoc_box_id or "", message_id=message_id)
    entities = message.get("Entities") or message.get("entities") or []
    for entity in sorted(entities, key=_entity_priority):
        if not _is_downloadable_entity(entity):
            continue
        entity_id = entity.get("EntityId") or entity.get("entityId")
        if not entity_id:
            continue
        result["documents_discovered"] += 1
        document = _get_existing_document(db, message_id, str(entity_id))
        if document and document.status in {"processed", "downloaded"}:
            if document.status == "downloaded" and not document.review_id:
                receiving = _find_message_receiving(db, document)
                if receiving is not None:
                    _attach_all_message_artifacts(db, receiving, document)
            result["documents_skipped"] += 1
            continue
        document = document or _new_document(event, entity, message_id, str(entity_id))
        db.add(document)
        db.commit()
        db.refresh(document)
        try:
            outcome = _process_document(
                db,
                client,
                document,
                entity,
                create_google_sheet=create_google_sheet,
            )
            result["artifacts_downloaded"] += outcome["artifacts_downloaded"]
            result["deliveries_recovered"] += outcome["deliveries_succeeded"]
            result["deliveries_failed"] += outcome["deliveries_failed"]
            if outcome["processed"]:
                result["documents_processed"] += 1
                if document.review_id and document.review_id not in result["review_ids"]:
                    result["review_ids"].append(document.review_id)
            else:
                result["documents_downloaded"] += 1
        except Exception as exc:  # noqa: BLE001 - one bad entity must not stop the feed
            _record_failure(db, document, exc)
            result["documents_failed"] += 1


def _process_document(
    db: Session,
    client: DiadocClient,
    document: DiadocDocument,
    entity: dict[str, Any],
    *,
    create_google_sheet: bool,
) -> dict[str, Any]:
    try:
        response = client.get_entity_content(
            box_id=document.box_id,
            message_id=document.message_id,
            entity_id=document.entity_id,
        )
    except DiadocContentTooLargeError as exc:
        raise PermanentDiadocDocumentError(str(exc)) from exc
    if not response.content:
        raise DiadocApiError("GetEntityContent V4 вернул пустое содержимое")
    _validate_entity_content(response.content, entity)
    original_path = _save_binary(
        document,
        response,
        entity=entity,
        artifact_kind="original",
    )
    document.content_path = str(original_path)
    document.filename = Path(original_path).name
    artifacts_downloaded = 1
    deliveries_succeeded = 0
    deliveries_failed = 0
    payload = None
    message_receiving = _find_message_receiving(db, document)
    is_xml = _looks_like_xml(response.content, original_path)
    is_formalized_xml = (
        is_xml
        and _is_formalized_business_entity(entity)
    )
    if is_formalized_xml:
        payload = parse_diadoc_invoice_xml(
            response.content,
            file_id=f"{document.message_id}:{document.entity_id}",
            file_url=str(original_path),
        )
        payload.parser_metadata["diadoc_document_key"] = (
            _document_key(document)
        )
    elif (
        message_receiving is None
        and settings.diadoc_parse_unstructured_attachments
        and _is_unstructured(original_path)
        and _is_unstructured_document_candidate(entity)
    ):
        payload = _parse_unstructured_document(original_path)
        payload.parser_metadata["diadoc_document_key"] = _document_key(document)
    if payload is not None:
        receiving = _transfer_to_verification(db, document, payload)
        document.review_id = receiving.id
        document.status = "processed"
        document.error_text = None
        _clear_retry_metadata(document)
        db.add(document)
        db.commit()
        _attach_all_message_artifacts(db, receiving, document)
        if create_google_sheet and settings.google_sheets_enabled:
            delivery = _ensure_delivery(db, document, "google_sheets")
            delivery_result = _execute_delivery(db, client, delivery)
            deliveries_succeeded += int(delivery_result["success"])
            deliveries_failed += int(not delivery_result["success"])
        if settings.diadoc_generate_print_form and is_formalized_xml:
            delivery = _ensure_delivery(db, document, "print_form")
            delivery_result = _execute_delivery(db, client, delivery)
            deliveries_succeeded += int(delivery_result["success"])
            deliveries_failed += int(not delivery_result["success"])
            artifacts_downloaded += delivery_result["artifacts_downloaded"]
    else:
        if message_receiving is not None:
            document.review_id = message_receiving.id
            _attach_file_to_receiving(db, message_receiving, original_path, "diadoc_attachment")
        document.status = "downloaded"
        document.error_text = None
        _clear_retry_metadata(document)
        db.add(document)
        db.commit()
    return {
        "processed": payload is not None,
        "artifacts_downloaded": artifacts_downloaded,
        "deliveries_succeeded": deliveries_succeeded,
        "deliveries_failed": deliveries_failed,
    }


def _ensure_delivery(
    db: Session, document: DiadocDocument, delivery_type: str
) -> DiadocDelivery:
    if delivery_type not in DELIVERY_TYPES:
        raise ValueError(f"Неизвестный тип доставки Диадок: {delivery_type}")
    delivery = (
        db.query(DiadocDelivery)
        .filter(
            DiadocDelivery.diadoc_document_id == document.id,
            DiadocDelivery.delivery_type == delivery_type,
        )
        .first()
    )
    if delivery is None:
        delivery = DiadocDelivery(
            diadoc_document_id=document.id,
            delivery_type=delivery_type,
            status="pending",
        )
        db.add(delivery)
        db.commit()
        db.refresh(delivery)
    return delivery


def _execute_delivery(
    db: Session, client: DiadocClient | None, delivery: DiadocDelivery
) -> dict[str, Any]:
    if delivery.status == "succeeded":
        return {"success": True, "artifacts_downloaded": 0}
    document = db.get(DiadocDocument, delivery.diadoc_document_id)
    if document is None:
        _record_delivery_failure(db, delivery, ValueError("Документ Диадок не найден"))
        return {"success": False, "artifacts_downloaded": 0}
    delivery.attempts += 1
    delivery.status = "in_progress"
    delivery.next_retry_at = None
    delivery.last_error = None
    db.add(delivery)
    db.commit()
    try:
        if delivery.delivery_type == "google_sheets":
            result, artifacts_downloaded = _deliver_google_sheet(db, document), 0
        elif delivery.delivery_type == "print_form":
            result, artifacts_downloaded = _deliver_print_form(
                db, client or DiadocClient(), document
            )
        else:
            raise ValueError(f"Неизвестный тип доставки: {delivery.delivery_type}")
        delivery = db.get(DiadocDelivery, delivery.id) or delivery
        delivery.status = "succeeded"
        delivery.last_error = None
        delivery.next_retry_at = None
        delivery.completed_at = _utcnow()
        delivery.result_json = json.dumps(result or {}, ensure_ascii=False, default=str)
        db.add(delivery)
        db.commit()
        return {"success": True, "artifacts_downloaded": artifacts_downloaded}
    except Exception as exc:  # noqa: BLE001 - delivery remains recoverable
        _record_delivery_failure(db, delivery, exc)
        return {"success": False, "artifacts_downloaded": 0}


def _deliver_google_sheet(db: Session, document: DiadocDocument) -> dict[str, Any]:
    if not document.review_id:
        raise ValueError("Для документа Диадок не создана карточка проверки")
    receiving = db.get(Receiving, document.review_id)
    if receiving is None:
        raise ValueError("Карточка проверки Диадок не найдена")
    return create_real_google_sheet_for_review(db, receiving, settings.public_api_base_url)


def _deliver_print_form(
    db: Session, client: DiadocClient, document: DiadocDocument
) -> tuple[dict[str, Any], int]:
    existing = (
        db.query(DiadocArtifact)
        .filter(
            DiadocArtifact.diadoc_document_id == document.id,
            DiadocArtifact.artifact_kind == "print_form",
        )
        .first()
    )
    receiving = db.get(Receiving, document.review_id) if document.review_id else None
    if existing is not None and Path(existing.content_path).exists():
        if receiving is not None:
            _attach_file_to_receiving(
                db, receiving, Path(existing.content_path), "diadoc_print_form"
            )
        return {"content_path": existing.content_path, "mode": "already_downloaded"}, 0
    print_form = client.generate_print_form(
        box_id=document.box_id,
        message_id=document.message_id,
        document_id=document.entity_id,
    )
    pdf_path = _save_binary(
        document,
        print_form,
        entity={"FileName": print_form.filename or "print-form.pdf"},
        artifact_kind="print_form",
        forced_extension=".pdf",
    )
    if receiving is not None:
        _attach_file_to_receiving(db, receiving, pdf_path, "diadoc_print_form")
    return {"content_path": str(pdf_path), "mode": "downloaded"}, 1


def _record_delivery_failure(
    db: Session, delivery: DiadocDelivery, exc: Exception
) -> None:
    db.rollback()
    delivery = db.get(DiadocDelivery, delivery.id) or delivery
    attempts = max(1, int(delivery.attempts or 0))
    delivery.status = (
        "dead_letter"
        if attempts >= settings.diadoc_retry_max_attempts
        else "failed"
    )
    delivery.last_error = str(exc)
    delivery.next_retry_at = _utcnow() + timedelta(
        seconds=settings.diadoc_retry_base_delay_seconds * (2 ** max(0, attempts - 1))
    )
    db.add(delivery)
    db.commit()


def _delivery_due(delivery: DiadocDelivery, now: datetime) -> bool:
    return delivery.next_retry_at is None or delivery.next_retry_at <= now


def _reset_document_retry(db: Session, document: DiadocDocument) -> None:
    metadata = _metadata(document)
    metadata.pop("retry", None)
    document.raw_metadata_json = json.dumps(metadata, ensure_ascii=False)
    document.status = "failed"
    document.error_text = None
    db.add(document)
    db.commit()

def _transfer_to_verification(
    db: Session,
    document: DiadocDocument,
    payload: InvoiceReviewCreateRequest,
) -> Receiving:
    if document.review_id:
        existing = db.get(Receiving, document.review_id)
        if existing is not None:
            return existing
    target = _find_message_receiving(db, document) or _find_order_receiving(db, payload)
    if target is None:
        return create_invoice_review(db, payload)
    _attach_payload_document(db, target, payload)
    if not target.order_items:
        return update_invoice_review(db, target.id, payload)
    compare_payload = CompareInvoiceRequest(
        invoice_number=payload.invoice_number,
        invoice_date=payload.invoice_date,
        supplier_legal_name=payload.supplier_legal_name or payload.supplier,
        items=[
            InvoiceItemIn(
                name=item.name,
                quantity=item.quantity,
                unit=item.unit,
                price=item.price,
                comment=item.comment,
            )
            for item in payload.items
        ],
    )
    return compare_invoice(db, target.id, compare_payload)


def _parse_unstructured_document(path: Path) -> InvoiceReviewCreateRequest:
    extraction = extract_invoice_document(
        str(path),
        path.name,
        extraction_method=settings.diadoc_unstructured_extraction_method,
    )
    if extraction.get("stop_recommended"):
        raise ValueError(extraction.get("error") or "Не удалось разобрать вложение Диадок")
    parsed = extraction.get("payload") or {}
    metadata = parsed.get("parser_metadata") or {}
    metadata["source_channel"] = "diadoc"
    metadata["source_path"] = str(path)
    return InvoiceReviewCreateRequest(
        file_id=path.name,
        file_type=path.suffix.lstrip(".") or "file",
        file_url=str(path),
        raw_text=extraction.get("raw_text"),
        request_id=f"DIADOC-{path.stem}",
        supplier=parsed.get("supplier"),
        supplier_legal_name=parsed.get("supplier_legal_name"),
        invoice_date=parsed.get("invoice_date"),
        invoice_number=parsed.get("invoice_number"),
        document_number=parsed.get("document_number"),
        venue=parsed.get("venue"),
        delivery_address=parsed.get("delivery_address"),
        document_form=parsed.get("document_form"),
        supplier_inn=parsed.get("supplier_inn"),
        shipper=parsed.get("shipper"),
        consignee=parsed.get("consignee"),
        recipient=parsed.get("recipient"),
        trade_point=parsed.get("trade_point"),
        warehouse=parsed.get("warehouse"),
        basis=parsed.get("basis"),
        total_sum=parsed.get("total_sum"),
        items=[RecognizedInvoiceItem(**item) for item in parsed.get("items") or []],
        parser_metadata=metadata,
    )



def _find_message_receiving(db: Session, document: DiadocDocument) -> Receiving | None:
    linked = (
        db.query(DiadocDocument)
        .filter(
            DiadocDocument.box_id == document.box_id,
            DiadocDocument.message_id == document.message_id,
            DiadocDocument.review_id.is_not(None),
            DiadocDocument.id != document.id,
        )
        .order_by(DiadocDocument.id.asc())
        .first()
    )
    return db.get(Receiving, linked.review_id) if linked and linked.review_id else None


def _entity_priority(
    entity: dict[str, Any],
) -> tuple[int, str]:
    filename = str(
        entity.get("FileName")
        or entity.get("fileName")
        or ""
    )
    if _is_formalized_business_entity(entity):
        return (0, filename)
    if _is_unstructured_document_candidate(entity):
        return (1, filename)
    return (2, filename)


def _find_order_receiving(db: Session, payload: InvoiceReviewCreateRequest) -> Receiving | None:
    candidates = _order_candidates(payload.basis, payload.parser_metadata)
    if not candidates:
        return None
    receivings = db.query(Receiving).filter(Receiving.order_number.in_(candidates)).all()
    for receiving in receivings:
        if receiving.order_items:
            return receiving
    return None


def _order_candidates(basis: str | None, metadata: dict[str, Any]) -> set[str]:
    result = set()
    for value in (basis, metadata.get("order_number"), metadata.get("purchase_order_number")):
        text = str(value or "").strip()
        if not text:
            continue
        result.add(text)
        result.update(re.findall(r"[A-Za-zА-Яа-я0-9][A-Za-zА-Яа-я0-9._/-]{2,}", text))
    return result


def _attach_payload_document(db: Session, receiving: Receiving, payload: InvoiceReviewCreateRequest) -> None:
    file_id = payload.file_id or payload.file_url
    exists = any(document.file_id == file_id for document in receiving.documents)
    if exists:
        return
    db.add(
        ReceivingDocument(
            receiving_id=receiving.id,
            file_id=file_id,
            file_type=payload.file_type,
            source="diadoc",
            file_url=payload.file_url,
            ocr_status="ocr_processed",
            raw_text=payload.raw_text,
            supplier_legal_name=payload.supplier_legal_name or payload.supplier,
            invoice_number=payload.invoice_number,
            invoice_date=payload.invoice_date,
        )
    )
    db.commit()


def _attach_all_message_artifacts(
    db: Session, receiving: Receiving, document: DiadocDocument
) -> None:
    message_documents = (
        db.query(DiadocDocument)
        .filter(
            DiadocDocument.box_id == document.box_id,
            DiadocDocument.message_id == document.message_id,
        )
        .all()
    )
    document_ids = [item.id for item in message_documents]
    artifacts = (
        db.query(DiadocArtifact)
        .filter(DiadocArtifact.diadoc_document_id.in_(document_ids))
        .all()
        if document_ids
        else []
    )
    for artifact in artifacts:
        _attach_file_to_receiving(
            db,
            receiving,
            Path(artifact.content_path),
            f"diadoc_{artifact.artifact_kind}",
        )
    for item in message_documents:
        if item.review_id is None:
            item.review_id = receiving.id
            db.add(item)
    db.commit()

def _attach_file_to_receiving(db: Session, receiving: Receiving, path: Path, source: str) -> None:
    file_id = str(path)
    exists = (
        db.query(ReceivingDocument)
        .filter(
            ReceivingDocument.receiving_id == receiving.id,
            ReceivingDocument.file_id == file_id,
        )
        .first()
    )
    if exists is not None:
        return
    db.add(
        ReceivingDocument(
            receiving_id=receiving.id,
            file_id=file_id,
            file_type=path.suffix.lstrip(".") or "file",
            source=source,
            file_url=file_id,
            ocr_status="stored",
        )
    )
    db.commit()


def _save_binary(
    document: DiadocDocument,
    response: DiadocBinaryResponse,
    *,
    entity: dict[str, Any],
    artifact_kind: str,
    forced_extension: str | None = None,
) -> Path:
    root = Path(settings.diadoc_documents_dir) / _safe_part(document.message_id)
    root.mkdir(parents=True, exist_ok=True)
    source_name = response.filename or entity.get("FileName") or entity.get("fileName") or document.filename
    extension = forced_extension or _guess_extension(str(source_name or ""), response.content_type, response.content)
    base = _safe_part(Path(str(source_name or document.entity_id)).stem) or _safe_part(document.entity_id)
    path = root / f"{base}-{_safe_part(document.entity_id)}-{artifact_kind}{extension}"
    path.write_bytes(response.content)
    session = object_session(document)
    if session is not None:
        existing = (
            session.query(DiadocArtifact)
            .filter(
                DiadocArtifact.diadoc_document_id == document.id,
                DiadocArtifact.artifact_kind == artifact_kind,
                DiadocArtifact.source_entity_id == document.entity_id,
            )
            .first()
        )
        if existing is None:
            existing = DiadocArtifact(
                diadoc_document_id=document.id,
                artifact_kind=artifact_kind,
                source_entity_id=document.entity_id,
                filename=path.name,
                content_type=response.content_type,
                content_path=str(path),
            )
        else:
            existing.filename = path.name
            existing.content_type = response.content_type
            existing.content_path = str(path)
        session.add(existing)
        session.commit()
    return path


def _record_failure(db: Session, document: DiadocDocument, exc: Exception) -> None:
    db.rollback()
    document = db.get(DiadocDocument, document.id) or document
    metadata = _metadata(document)
    retry = metadata.setdefault("retry", {})
    attempts = int(retry.get("attempts") or 0) + 1
    permanent = isinstance(exc, PermanentDiadocDocumentError)
    if permanent:
        attempts = max(
            attempts,
            settings.diadoc_retry_max_attempts,
        )
    retry["attempts"] = attempts
    retry["permanent"] = permanent
    retry["last_error_at"] = _utcnow().isoformat()
    retry["next_retry_at"] = (
        _utcnow()
        + timedelta(
            seconds=(
                settings.diadoc_retry_base_delay_seconds
                * (2 ** max(0, attempts - 1))
            )
        )
    ).isoformat()
    document.status = (
        "dead_letter"
        if attempts >= settings.diadoc_retry_max_attempts
        else "failed"
    )
    document.error_text = str(exc)
    document.raw_metadata_json = json.dumps(metadata, ensure_ascii=False)
    db.add(document)
    db.commit()


def _retry_due(metadata: dict[str, Any], now: datetime) -> bool:
    value = (metadata.get("retry") or {}).get("next_retry_at")
    if not value:
        return True
    try:
        return datetime.fromisoformat(value) <= now
    except ValueError:
        return True


def _clear_retry_metadata(document: DiadocDocument) -> None:
    metadata = _metadata(document)
    metadata.pop("retry", None)
    document.raw_metadata_json = json.dumps(metadata, ensure_ascii=False)


def _append_metadata_error(document: DiadocDocument, key: str, value: str) -> None:
    metadata = _metadata(document)
    metadata[key] = value
    document.raw_metadata_json = json.dumps(metadata, ensure_ascii=False)


def _metadata(document: DiadocDocument) -> dict[str, Any]:
    try:
        value = json.loads(document.raw_metadata_json or "{}")
    except json.JSONDecodeError:
        value = {}
    return value if isinstance(value, dict) else {}


def _acquire_sync_lease(db: Session) -> bool:
    now = _utcnow()
    expires_at = now + timedelta(seconds=max(60, settings.diadoc_sync_lease_seconds))
    lease = db.query(DiadocLease).filter(DiadocLease.name == _SYNC_LEASE_NAME).first()
    if lease is None:
        try:
            db.add(
                DiadocLease(
                    name=_SYNC_LEASE_NAME,
                    owner_id=_SYNC_OWNER_ID,
                    expires_at=expires_at,
                )
            )
            db.commit()
            return True
        except IntegrityError:
            db.rollback()
    updated = (
        db.query(DiadocLease)
        .filter(
            DiadocLease.name == _SYNC_LEASE_NAME,
            or_(
                DiadocLease.expires_at <= now,
                DiadocLease.owner_id == _SYNC_OWNER_ID,
            ),
        )
        .update(
            {
                DiadocLease.owner_id: _SYNC_OWNER_ID,
                DiadocLease.expires_at: expires_at,
            },
            synchronize_session=False,
        )
    )
    db.commit()
    return updated == 1



def _renew_sync_lease(db: Session) -> None:
    expires_at = _utcnow() + timedelta(
        seconds=max(60, settings.diadoc_sync_lease_seconds)
    )
    updated = (
        db.query(DiadocLease)
        .filter(
            DiadocLease.name == _SYNC_LEASE_NAME,
            DiadocLease.owner_id == _SYNC_OWNER_ID,
        )
        .update(
            {DiadocLease.expires_at: expires_at},
            synchronize_session=False,
        )
    )
    db.commit()
    if updated != 1:
        raise RuntimeError(
            "Потеряна блокировка синхронизации Диадок"
        )

def _release_sync_lease(db: Session) -> None:
    try:
        db.rollback()
        db.query(DiadocLease).filter(
            DiadocLease.name == _SYNC_LEASE_NAME,
            DiadocLease.owner_id == _SYNC_OWNER_ID,
        ).update(
            {DiadocLease.expires_at: _utcnow()},
            synchronize_session=False,
        )
        db.commit()
    except Exception:  # noqa: BLE001 - an expired lease is self-healing
        db.rollback()


def _document_key(document: DiadocDocument) -> str:
    return f"{document.box_id}:{document.message_id}:{document.entity_id}"


def _validate_configuration() -> None:
    if not settings.diadoc_integration_enabled:
        raise ValueError("Интеграция Диадок выключена")
    if not settings.diadoc_box_id:
        raise ValueError("Заполните DIADOC_BOX_ID")
    has_static_token = bool(settings.diadoc_access_token)
    has_refresh_credentials = bool(
        settings.diadoc_refresh_token
        and settings.diadoc_client_id
        and settings.diadoc_client_secret
    )
    if not has_static_token and not has_refresh_credentials:
        raise ValueError(
            "Выполните Diadoc OAuth или заполните DIADOC_ACCESS_TOKEN; "
            "для refresh token также нужны DIADOC_CLIENT_ID и DIADOC_CLIENT_SECRET"
        )


def _empty_result(*, status: str = "ok") -> dict[str, Any]:
    return {
        "status": status,
        "pages_received": 0,
        "events_received": 0,
        "documents_discovered": 0,
        "documents_processed": 0,
        "documents_downloaded": 0,
        "documents_skipped": 0,
        "documents_failed": 0,
        "documents_retried": 0,
        "documents_recovered": 0,
        "deliveries_retried": 0,
        "deliveries_recovered": 0,
        "deliveries_failed": 0,
        "artifacts_downloaded": 0,
        "review_ids": [],
        "next_index_key_present": False,
    }


def _get_or_create_state(db: Session, box_id: str) -> DiadocSyncState:
    state = db.query(DiadocSyncState).filter(DiadocSyncState.box_id == box_id).first()
    if state is None:
        state = DiadocSyncState(box_id=box_id)
        db.add(state)
        db.commit()
        db.refresh(state)
    return state


def _get_existing_document(db: Session, message_id: str, entity_id: str) -> DiadocDocument | None:
    return (
        db.query(DiadocDocument)
        .filter(
            DiadocDocument.box_id == settings.diadoc_box_id,
            DiadocDocument.message_id == message_id,
            DiadocDocument.entity_id == entity_id,
        )
        .first()
    )


def _new_document(
    event: dict[str, Any], entity: dict[str, Any], message_id: str, entity_id: str
) -> DiadocDocument:
    return DiadocDocument(
        box_id=settings.diadoc_box_id or "",
        event_id=_find_first_value(event, "EventId", "eventId"),
        index_key=_find_first_value(event, "IndexKey", "indexKey"),
        message_id=message_id,
        entity_id=entity_id,
        document_type=_entity_type(entity),
        document_function=entity.get("Function") or entity.get("function"),
        document_version=entity.get("Version") or entity.get("version"),
        filename=entity.get("FileName") or entity.get("fileName"),
        status="discovered",
        raw_metadata_json=json.dumps(entity, ensure_ascii=False),
    )


def _is_downloadable_entity(
    entity: dict[str, Any],
) -> bool:
    entity_type = str(
        entity.get("EntityType")
        or entity.get("entityType")
        or ""
    )
    if entity_type.casefold() == "attachment":
        return (
            settings.diadoc_download_all_attachments
            or _is_document_entity(entity)
        )
    return _is_document_entity(entity)


def _is_document_entity(
    entity: dict[str, Any],
) -> bool:
    return (
        _is_formalized_business_entity(entity)
        or _is_unstructured_document_candidate(entity)
    )


def _is_formalized_business_entity(
    entity: dict[str, Any],
) -> bool:
    if _is_service_entity(entity):
        return False
    values = _entity_descriptor_values(entity)
    combined = " ".join(values).casefold()
    return any(
        marker.casefold() in combined
        for marker in SUPPORTED_TYPE_MARKERS
    )


def _is_unstructured_document_candidate(
    entity: dict[str, Any],
) -> bool:
    if _is_service_entity(entity):
        return False
    filename = str(
        entity.get("FileName")
        or entity.get("fileName")
        or ""
    )
    suffix = Path(filename).suffix.casefold()
    if suffix not in UNSTRUCTURED_EXTENSIONS:
        return False
    attachment_type = str(
        entity.get("AttachmentType")
        or entity.get("attachmentType")
        or ""
    ).casefold()
    return (
        not attachment_type
        or attachment_type == "nonformalized"
        or bool(
            entity.get("DocumentInfo")
            or entity.get("documentInfo")
        )
    )


def _is_service_entity(
    entity: dict[str, Any],
) -> bool:
    combined = " ".join(
        _entity_descriptor_values(entity)
    ).casefold()
    return any(
        marker.casefold() in combined
        for marker in SERVICE_ATTACHMENT_MARKERS
    )


def _entity_descriptor_values(
    entity: dict[str, Any],
) -> list[str]:
    document_info = (
        entity.get("DocumentInfo")
        or entity.get("documentInfo")
        or {}
    )
    values = []
    for source in (entity, document_info):
        for key in (
            "TypeNamedId",
            "typeNamedId",
            "DocumentType",
            "documentType",
            "AttachmentType",
            "attachmentType",
            "ContentTypeId",
            "contentTypeId",
            "FileName",
            "fileName",
        ):
            value = source.get(key)
            if value not in (None, ""):
                values.append(str(value))
    return values


def _entity_type(entity: dict[str, Any]) -> str | None:
    return (
        entity.get("TypeNamedId")
        or entity.get("typeNamedId")
        or entity.get("AttachmentType")
        or entity.get("attachmentType")
        or entity.get("EntityType")
        or entity.get("entityType")
    )


def _entity_content(entity: dict[str, Any]) -> bytes | None:
    content = entity.get("Content") or entity.get("content") or {}
    data = content.get("Data") or content.get("data")
    if not data:
        return None
    try:
        return base64.b64decode(data)
    except (ValueError, TypeError):
        return None


def _find_entity(payload: dict[str, Any], entity_id: str) -> dict[str, Any] | None:
    entities = payload.get("Entities") or payload.get("entities") or []
    for entity in entities:
        current_id = entity.get("EntityId") or entity.get("entityId")
        if current_id == entity_id:
            return entity
    return None



def _validate_entity_content(
    content: bytes,
    entity: dict[str, Any],
) -> None:
    maximum = max(1, settings.diadoc_max_attachment_bytes)
    if len(content) > maximum:
        raise PermanentDiadocDocumentError(
            "Вложение Диадок превышает допустимый размер "
            f"{maximum} байт"
        )
    encrypted = (
        entity.get("IsEncryptedContent")
        or entity.get("isEncryptedContent")
    )
    if encrypted:
        raise PermanentDiadocDocumentError(
            "Зашифрованное вложение Диадок не поддерживается"
        )
    stripped = content.lstrip()
    if not stripped.startswith(b"<"):
        return
    xml_maximum = max(1, settings.diadoc_max_xml_bytes)
    if len(content) > xml_maximum:
        raise PermanentDiadocDocumentError(
            "XML Диадок превышает допустимый размер "
            f"{xml_maximum} байт"
        )
    upper_prefix = stripped[:4096].upper()
    if b"<!DOCTYPE" in upper_prefix or b"<!ENTITY" in upper_prefix:
        raise PermanentDiadocDocumentError(
            "XML Диадок содержит запрещённое объявление DTD/ENTITY"
        )

def _looks_like_xml(content: bytes, path: Path) -> bool:
    return path.suffix.casefold() == ".xml" or content.lstrip().startswith(b"<")


def _is_unstructured(path: Path) -> bool:
    return path.suffix.casefold() in UNSTRUCTURED_EXTENSIONS


def _guess_extension(filename: str, content_type: str | None, content: bytes) -> str:
    suffix = Path(filename).suffix
    if suffix and len(suffix) <= 10:
        return suffix.casefold()
    if content.lstrip().startswith(b"<"):
        return ".xml"
    if content.startswith(b"%PDF"):
        return ".pdf"
    guessed = mimetypes.guess_extension((content_type or "").split(";", 1)[0].strip())
    return guessed or ".bin"


def _safe_part(value: str) -> str:
    return re.sub(r"[^A-Za-zА-Яа-я0-9._-]+", "_", value).strip("._")[:160]


def _last_index_key(events: list[dict[str, Any]]) -> str | None:
    for event in reversed(events):
        value = event.get("IndexKey") or event.get("indexKey")
        if value:
            return str(value)
    return None


def _find_first_value(payload: Any, *keys: str) -> str | None:
    if isinstance(payload, dict):
        for key in keys:
            value = payload.get(key)
            if value not in (None, ""):
                return str(value)
        for value in payload.values():
            found = _find_first_value(value, *keys)
            if found:
                return found
    elif isinstance(payload, list):
        for value in payload:
            found = _find_first_value(value, *keys)
            if found:
                return found
    return None
