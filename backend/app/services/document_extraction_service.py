import json
import re
import shlex
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any
from uuid import uuid4

from app.config import settings
from app.schemas.document_evidence import (
    DocumentEvidence,
    EvidencePageSource,
    EvidenceProviderAttempt,
)
from app.services.document_image_preparation_service import prepare_document_page
from app.services.invoice_parser_service import extract_invoice_payload_with_fallback
from app.services.openai_invoice_parser_service import OpenAIInvoiceParserError, parse_invoice_with_openai
from app.services.ocr_service import OcrConfigurationError, OcrProviderError, recognize_invoice_image
from app.services.provider_health_service import mineru_health


class DocumentExtractionError(RuntimeError):
    pass


DEFAULT_MINERU_COMMAND = (
    "{python_executable} -m mineru.cli.client "
    "-p {file_path} -o {output_dir} -b pipeline -l cyrillic"
)


def extract_invoice_document(
    file_path: str,
    fallback_filename: str | None = None,
    extraction_method: str | None = None,
    on_log: Any | None = None,
    *,
    _evidence_override: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Extract evidence and return a normalized invoice payload."""
    backend = _resolve_extraction_method(extraction_method)
    pipeline_logs: list[dict[str, Any]] = []
    _append_pipeline_log(
        pipeline_logs,
        _pipeline_log(
            "document_received",
            "ok",
            "Документ получен backend-сервисом.",
            selected_method=backend,
            filename=fallback_filename or Path(file_path).name,
            source_type=_source_type(file_path, fallback_filename),
        ),
        on_log=on_log,
    )
    if backend == "openai":
        _append_pipeline_log(
            pipeline_logs,
            _pipeline_log("collect_evidence_start", "running", "Начат сбор evidence для OpenAI parser."),
            on_log=on_log,
        )
        if _evidence_override is None:
            evidence = _collect_openai_evidence(
                file_path,
                fallback_filename,
                on_attempt=lambda attempt: _append_pipeline_log(
                    pipeline_logs,
                    _provider_attempt_log(attempt),
                    on_log=on_log,
                ),
            )
        else:
            evidence = _evidence_override
            for attempt in evidence.get("provider_attempts") or []:
                _append_pipeline_log(
                    pipeline_logs,
                    _provider_attempt_log(attempt),
                    on_log=None,
                )
        _append_pipeline_log(
            pipeline_logs,
            _pipeline_log(
                "collect_evidence_complete",
                "ok" if _evidence_has_content(evidence) else "warning",
                "Evidence собран для OpenAI parser."
                if _evidence_has_content(evidence)
                else "Evidence для OpenAI parser пустой.",
                extraction_method=evidence.get("extraction_method"),
                ocr_used=bool(evidence.get("ocr_used")),
                raw_text_length=len(evidence.get("raw_text") or ""),
                pages=evidence.get("pages"),
                source_type=evidence.get("source_type"),
                evidence_version=evidence.get("evidence_version"),
                provider_attempts=len(evidence.get("provider_attempts") or []),
            ),
            on_log=on_log,
        )
        if not _evidence_has_content(evidence):
            error_message = (
                "Перед OpenAI parser не удалось получить текст или структурированный evidence. "
                "Процесс остановлен."
            )
            _append_pipeline_log(
                pipeline_logs,
                _pipeline_log(
                    "openai_skipped_empty_evidence",
                    "error",
                    error_message,
                    recommendation=(
                        "Попробуйте прогнать документ полностью через ИИ после устранения проблем "
                        "с OCR/MinerU или загрузите более качественный файл."
                    ),
                ),
                on_log=on_log,
            )
            manual_result = _manual_review_result(error_message, fallback_filename)
            manual_result.update(
                {
                    "provider": "openai_empty_evidence",
                    "raw_text": evidence.get("raw_text") or "",
                    "pages": evidence.get("pages"),
                    "selected_method": backend,
                    "evidence": evidence,
                    "pipeline_logs": pipeline_logs,
                    "stop_recommended": True,
                    "error": evidence.get("error") or error_message,
                    "retry_recommended_method": "openai",
                    "retry_recommended_label": "OpenAI vision parser",
                }
            )
            return manual_result
        _append_pipeline_log(
            pipeline_logs,
            _pipeline_log(
                "openai_request_start",
                "running",
                "Отправляю evidence в OpenAI parser.",
                model=settings.openai_invoice_model,
                raw_text_length=len(evidence.get("raw_text") or ""),
            ),
            on_log=on_log,
        )
        try:
            payload = parse_invoice_with_openai(evidence)
        except OpenAIInvoiceParserError as exc:
            _append_pipeline_log(
                pipeline_logs,
                _pipeline_log(
                    "openai_request_failed",
                    "error",
                    "OpenAI parser завершился ошибкой.",
                    error=str(exc),
                ),
                on_log=on_log,
            )
            manual_result = _manual_review_result(str(exc), fallback_filename)
            manual_result.update(
                {
                    "provider": "openai_error",
                    "raw_text": evidence.get("raw_text") or "",
                    "pages": evidence.get("pages"),
                    "selected_method": backend,
                    "evidence": evidence,
                    "pipeline_logs": pipeline_logs,
                    "stop_recommended": True,
                    "retry_recommended_method": "openai",
                    "retry_recommended_label": "OpenAI vision parser",
                }
            )
            return manual_result
        validation_errors = _payload_validation_errors(payload)
        has_useful_payload = not validation_errors
        empty_payload = not _payload_has_useful_data(payload)
        result_error = None
        if empty_payload:
            result_error = "OpenAI parser вернул пустой структурированный JSON."
        elif validation_errors:
            result_error = "OpenAI parser вернул неполный результат: " + "; ".join(validation_errors)
        _append_pipeline_log(
            pipeline_logs,
            _pipeline_log(
                "openai_request_complete",
                "ok" if has_useful_payload else "error",
                "OpenAI parser вернул структурированный результат."
                if has_useful_payload
                else "OpenAI parser вернул пустой структурированный JSON.",
                parser_provider=payload.get("parser_provider"),
                items_count=len(payload.get("items") or []),
                supplier=payload.get("supplier"),
                invoice_number=payload.get("invoice_number"),
                validation_errors=validation_errors,
            ),
            on_log=on_log,
        )
        return {
            "provider": "openai",
            "raw_text": evidence.get("raw_text") or "",
            "confidence": None,
            "pages": evidence.get("pages"),
            "payload": payload,
            "parser_provider": "openai",
            "parser_notes": payload.get("parser_notes", []),
            "selected_method": backend,
            "evidence": evidence,
            "pipeline_logs": pipeline_logs,
            "stop_recommended": not has_useful_payload,
            "validation_errors": validation_errors,
            "retry_recommended_method": None,
            "retry_recommended_label": None,
            "error": result_error,
        }

    if backend in {"mineru", "hybrid"}:
        _append_pipeline_log(
            pipeline_logs,
            _pipeline_log("mineru_start", "running", "Запускаю извлечение через MinerU."),
            on_log=on_log,
        )
        mineru_result, mineru_error = _run_mineru_with_health_guard(
            file_path,
            fallback_filename,
            pipeline_logs=pipeline_logs,
            on_log=on_log,
        )
        if mineru_result and _payload_has_useful_data(mineru_result["payload"]):
            mineru_result["selected_method"] = backend
            mineru_result["pipeline_logs"] = pipeline_logs
            return mineru_result

        if backend == "hybrid":
            _append_pipeline_log(
                pipeline_logs,
                _pipeline_log("ocr_fallback_start", "running", "Запускаю OCR fallback после MinerU."),
                on_log=on_log,
            )
            ocr_result = _extract_with_ocr(file_path, fallback_filename)
            if mineru_error:
                ocr_result["error"] = mineru_error
                ocr_result["parser_notes"] = [*ocr_result.get("parser_notes", []), f"MinerU fallback: {mineru_error}"]
            ocr_result["selected_method"] = backend
            _append_pipeline_log(
                pipeline_logs,
                _pipeline_log(
                    "ocr_fallback_complete",
                    "ok" if _payload_has_useful_data(ocr_result.get("payload") or {}) else "warning",
                    "OCR fallback завершен.",
                    provider=ocr_result.get("provider"),
                    raw_text_length=len(ocr_result.get("raw_text") or ""),
                    items_count=len((ocr_result.get("payload") or {}).get("items") or []),
                ),
                on_log=on_log,
            )
            ocr_result["pipeline_logs"] = pipeline_logs
            if not _payload_has_useful_data(ocr_result.get("payload") or {}):
                ocr_result["stop_recommended"] = True
                ocr_result["retry_recommended_method"] = "openai"
                ocr_result["retry_recommended_label"] = "OpenAI vision parser"
            return ocr_result

        manual_result = _manual_review_result(
            mineru_error or "MinerU extraction returned an empty payload and OCR fallback is disabled.",
            fallback_filename,
        )
        manual_result["selected_method"] = backend
        manual_result["pipeline_logs"] = pipeline_logs
        manual_result["stop_recommended"] = True
        manual_result["retry_recommended_method"] = "openai"
        manual_result["retry_recommended_label"] = "OpenAI vision parser"
        return manual_result

    _append_pipeline_log(
        pipeline_logs,
        _pipeline_log("ocr_start", "running", "Запускаю OCR extraction."),
        on_log=on_log,
    )
    ocr_result = _extract_with_ocr(file_path, fallback_filename)
    ocr_result["selected_method"] = backend
    _append_pipeline_log(
        pipeline_logs,
        _pipeline_log(
            "ocr_complete",
            "ok" if _payload_has_useful_data(ocr_result.get("payload") or {}) else "warning",
            "OCR extraction завершен.",
            provider=ocr_result.get("provider"),
            raw_text_length=len(ocr_result.get("raw_text") or ""),
            items_count=len((ocr_result.get("payload") or {}).get("items") or []),
        ),
        on_log=on_log,
    )
    ocr_result["pipeline_logs"] = pipeline_logs
    validation_errors = _payload_validation_errors(ocr_result.get("payload") or {})
    if validation_errors:
        ocr_result["stop_recommended"] = True
        ocr_result["validation_errors"] = validation_errors
        ocr_result["error"] = ocr_result.get("error") or "; ".join(validation_errors)
        ocr_result["retry_recommended_method"] = "openai"
        ocr_result["retry_recommended_label"] = "OpenAI vision parser"
    return ocr_result


def extract_invoice_document_set(
    file_paths: list[str],
    fallback_filenames: list[str] | None = None,
    extraction_method: str | None = None,
    on_log: Any | None = None,
) -> dict[str, Any]:
    if not file_paths:
        raise DocumentExtractionError("At least one document page is required.")
    filenames = fallback_filenames or [Path(path).name for path in file_paths]
    if len(filenames) != len(file_paths):
        raise DocumentExtractionError("file_paths and fallback_filenames must have equal length.")
    if len(file_paths) == 1:
        return extract_invoice_document(
            file_paths[0],
            filenames[0],
            extraction_method=extraction_method,
            on_log=on_log,
        )
    backend = _resolve_extraction_method(extraction_method)
    if backend != "openai":
        message = "Многостраничный документ поддерживается только в режиме OpenAI vision parser."
        return {
            **_manual_review_result(message, filenames[0]),
            "selected_method": backend,
            "stop_recommended": True,
            "error": message,
            "pipeline_logs": [
                _pipeline_log(
                    "multipage_method_rejected",
                    "error",
                    message,
                    pages=len(file_paths),
                    selected_method=backend,
                )
            ],
        }

    page_evidence = []
    for page_number, (path, filename) in enumerate(zip(file_paths, filenames, strict=True), start=1):
        if on_log is not None:
            on_log(
                _pipeline_log(
                    "document_page_start",
                    "running",
                    f"Начата подготовка страницы {page_number}.",
                    page_number=page_number,
                    filename=filename,
                )
            )
        evidence = _collect_openai_evidence(
            path,
            filename,
            on_attempt=lambda attempt, current_page=page_number: on_log(
                _provider_attempt_log({**attempt, "page_number": current_page})
            )
            if on_log is not None
            else None,
        )
        page_evidence.append(evidence)
        if on_log is not None:
            on_log(
                _pipeline_log(
                    "document_page_complete",
                    "ok" if _evidence_has_content(evidence) else "warning",
                    f"Страница {page_number} подготовлена.",
                    page_number=page_number,
                    raw_text_length=len(evidence.get("raw_text") or ""),
                )
            )

    combined = _merge_page_evidence(page_evidence, filenames)
    return extract_invoice_document(
        file_paths[0],
        f"{len(file_paths)} pages: {filenames[0]}",
        extraction_method="openai",
        on_log=on_log,
        _evidence_override=combined,
    )


def _merge_page_evidence(
    page_evidence: list[dict[str, Any]],
    filenames: list[str],
) -> dict[str, Any]:
    logical_document_id = f"document-{uuid4().hex}"
    page_sources = []
    attempts = []
    errors = []
    raw_parts = []
    structured_parts = []
    methods = []
    page_header_hints = []
    for page_number, evidence in enumerate(page_evidence, start=1):
        raw_text = evidence.get("raw_text") or ""
        if raw_text.strip():
            raw_parts.append(f"--- Страница {page_number}: {filenames[page_number - 1]} ---\n{raw_text}")
        structured = evidence.get("structured_document")
        if structured not in (None, "", [], {}):
            structured_parts.append({"page_number": page_number, "content": structured})
        for source in evidence.get("page_sources") or []:
            if isinstance(source, dict):
                page_sources.append({**source, "page_number": page_number})
        for attempt in evidence.get("provider_attempts") or []:
            if isinstance(attempt, dict):
                attempts.append({**attempt, "page_number": page_number})
        errors.extend(str(error) for error in (evidence.get("errors") or []) if error)
        method = evidence.get("extraction_method")
        if method and method not in methods:
            methods.append(method)
        if raw_text.strip():
            hint = extract_invoice_payload_with_fallback(
                raw_text,
                filenames[page_number - 1],
            )
            page_marker = _extract_page_marker_hint(raw_text)
            page_header_hints.append(
                {
                    "page_number": page_number,
                    "invoice_number": hint.get("invoice_number"),
                    "supplier_inn": hint.get("supplier_inn"),
                    "page_marker_current": page_marker.get("current"),
                    "page_marker_total": page_marker.get("total"),
                }
            )

    source_types = {evidence.get("source_type") for evidence in page_evidence}
    consistency_warnings = _page_consistency_warnings(page_header_hints)
    merged = DocumentEvidence(
        logical_document_id=logical_document_id,
        filename=f"{len(page_evidence)} pages",
        source_type=source_types.pop() if len(source_types) == 1 else "unknown",
        ocr_used=any(bool(evidence.get("ocr_used")) for evidence in page_evidence),
        extraction_method="+".join(methods) or "source_images",
        raw_text="\n\n".join(raw_parts),
        structured_document=structured_parts or None,
        pages=len(page_evidence),
        page_sources=page_sources,
        provider_attempts=attempts,
        errors=errors,
        consistency_warnings=consistency_warnings,
        error="; ".join(errors) if errors else None,
    )
    return merged.model_dump(mode="json")


def _page_consistency_warnings(page_header_hints: list[dict[str, Any]]) -> list[str]:
    warnings = []
    for field, label in (
        ("invoice_number", "номера документа"),
        ("supplier_inn", "ИНН поставщика"),
    ):
        values = {
            str(hint.get(field)).strip()
            for hint in page_header_hints
            if hint.get(field) not in (None, "")
        }
        if len(values) > 1:
            warnings.append(
                f"На страницах найдены разные значения {label}: {', '.join(sorted(values))}."
            )
    marker_currents = {
        int(hint.get("page_marker_current"))
        for hint in page_header_hints
        if hint.get("page_marker_current") not in (None, "")
    }
    marker_totals = {
        int(hint.get("page_marker_total"))
        for hint in page_header_hints
        if hint.get("page_marker_total") not in (None, "")
    }
    if marker_totals:
        declared_total = max(marker_totals)
        actual_total = len(page_header_hints)
        if declared_total > actual_total:
            warnings.append(
                f"Маркер страниц документа указывает минимум на {declared_total} стр., но загружено только {actual_total}."
            )
    if marker_currents:
        expected = set(range(1, max(marker_currents) + 1))
        if expected - marker_currents:
            missing = ", ".join(str(value) for value in sorted(expected - marker_currents))
            warnings.append(f"В маркерах страниц пропущены страницы: {missing}.")
    return warnings


def _extract_page_marker_hint(raw_text: str) -> dict[str, int | None]:
    normalized = str(raw_text or "")
    numbered_total = re.search(
        r"(?:страниц[аы]?|лист)\s*[№:]?\s*(\d{1,2})\s*(?:из|/)\s*(\d{1,2})",
        normalized,
        flags=re.IGNORECASE,
    )
    if numbered_total:
        return {
            "current": int(numbered_total.group(1)),
            "total": int(numbered_total.group(2)),
        }
    numbered = re.search(
        r"(?:страниц[аы]?|лист)\s*[№:]?\s*(\d{1,2})\b",
        normalized,
        flags=re.IGNORECASE,
    )
    if numbered:
        return {"current": int(numbered.group(1)), "total": None}
    return {"current": None, "total": None}


def _resolve_extraction_method(extraction_method: str | None) -> str:
    method = (extraction_method or "").strip().lower()
    normalized = {
        "openai": "openai",
        "google_ocr": "google_ocr",
        "ocr": "google_ocr",
        "mineru": "mineru",
        "hybrid": "hybrid",
    }.get(method)
    if normalized:
        return normalized

    configured_backend = (settings.document_extraction_backend or "openai").strip().lower()
    if configured_backend == "openai":
        return "openai"
    if configured_backend == "mineru":
        return "hybrid" if settings.document_extraction_fallback_to_ocr else "mineru"
    return "google_ocr"


def _collect_openai_evidence(
    file_path: str,
    fallback_filename: str | None,
    *,
    on_attempt: Any | None = None,
) -> dict[str, Any]:
    source_type = _source_type(file_path, fallback_filename)
    filename = fallback_filename or Path(file_path).name
    evidence = DocumentEvidence(
        logical_document_id=f"document-{uuid4().hex}",
        filename=filename,
        source_type=source_type,
        page_sources=[
            EvidencePageSource(
                page_number=1,
                filename=filename,
                source_type=source_type,
                original_path=file_path,
            )
        ],
    )
    evidence_input_path = file_path
    if source_type == "image":
        _notify_evidence_attempt_start("image_preparation", on_attempt)
        started = time.perf_counter()
        try:
            prepared = prepare_document_page(file_path)
            page = evidence.page_sources[0]
            page.prepared_path = prepared.get("prepared_path")
            page.transformations = prepared.get("transformations") or []
            page.quality = prepared.get("quality") or {}
            for reason in page.quality.get("review_reasons") or []:
                evidence.consistency_warnings.append(f"Страница {page.page_number}: {reason}")
            for reason in page.quality.get("stop_reasons") or []:
                evidence.consistency_warnings.append(f"Страница {page.page_number}: {reason}")
            evidence_input_path = page.prepared_path or file_path
            _add_evidence_attempt(
                evidence,
                EvidenceProviderAttempt(
                    provider="image_preparation",
                    status="success",
                    duration_ms=_duration_ms(started),
                ),
                on_attempt,
            )
        except Exception as exc:  # noqa: BLE001 - original image remains a safe fallback
            message = f"Image preparation failed: {exc}"
            evidence.errors.append(message)
            _add_evidence_attempt(
                evidence,
                EvidenceProviderAttempt(
                    provider="image_preparation",
                    status="error",
                    duration_ms=_duration_ms(started),
                    error_type=type(exc).__name__,
                    error_message=message,
                ),
                on_attempt,
            )

    if source_type == "pdf":
        _notify_evidence_attempt_start("pdf_text", on_attempt)
        started = time.perf_counter()
        pdf_text = _extract_pdf_text(file_path)
        attempt = EvidenceProviderAttempt(
            provider="pdf_text",
            status="success" if pdf_text.strip() else "skipped",
            duration_ms=_duration_ms(started),
            raw_text_length=len(pdf_text),
        )
        _add_evidence_attempt(evidence, attempt, on_attempt)
        if pdf_text.strip():
            evidence.raw_text = pdf_text
            evidence.extraction_method = "pdf_text"
            return evidence.model_dump(mode="json")

    _notify_evidence_attempt_start("google_drive_ocr", on_attempt)
    started = time.perf_counter()
    ocr_result = _extract_with_ocr(evidence_input_path, fallback_filename)
    raw_text = ocr_result.get("raw_text") or ""
    ocr_error = ocr_result.get("error")
    _add_evidence_attempt(
        evidence,
        EvidenceProviderAttempt(
            provider=ocr_result.get("provider") or "google_drive_ocr",
            status="error" if ocr_error else ("success" if raw_text.strip() else "skipped"),
            duration_ms=_duration_ms(started),
            attempts=int(ocr_result.get("provider_attempts") or 1),
            retryable=bool(ocr_result.get("provider_retryable")),
            raw_text_length=len(raw_text),
            pages=ocr_result.get("pages"),
            error_type=ocr_result.get("error_type"),
            error_message=ocr_error,
        ),
        on_attempt,
    )
    if ocr_error:
        evidence.errors.append(ocr_error)
    if raw_text.strip():
        evidence.raw_text = raw_text
        evidence.extraction_method = ocr_result.get("provider") or "google_drive_ocr"
        evidence.ocr_used = True
        evidence.pages = ocr_result.get("pages")
        return evidence.model_dump(mode="json")

    _notify_evidence_attempt_start("mineru", on_attempt)
    started = time.perf_counter()
    health = mineru_health()
    health_reason = str(health.get("reason") or "").strip()
    cache_is_incomplete = "model cache is incomplete" in health_reason.casefold()
    if not health.get("ready") and cache_is_incomplete:
        mineru_result = None
        evidence.errors.append(health_reason)
        _add_evidence_attempt(
            evidence,
            EvidenceProviderAttempt(
                provider="mineru",
                status="skipped",
                duration_ms=_duration_ms(started),
                error_message=health_reason,
            ),
            on_attempt,
        )
    else:
        try:
            # The local health probe is advisory when the adapter can run in another
            # worker or is provided by a test double. A broken model cache is the
            # exception because launching MinerU in that state is predictably unsafe.
            mineru_result = _extract_with_mineru(evidence_input_path, fallback_filename)
        except Exception as exc:  # noqa: BLE001 - OCR/image evidence remains the fallback
            mineru_result = None
            error_message = str(exc) or health_reason or "MinerU extraction failed."
            evidence.errors.append(error_message)
            _add_evidence_attempt(
                evidence,
                EvidenceProviderAttempt(
                    provider="mineru",
                    status="skipped" if not health.get("ready") else "error",
                    duration_ms=_duration_ms(started),
                    error_type=type(exc).__name__,
                    error_message=error_message,
                ),
                on_attempt,
            )
    if mineru_result and (mineru_result.get("raw_text") or "").strip():
        mineru_text = mineru_result.get("raw_text") or ""
        _add_evidence_attempt(
            evidence,
            EvidenceProviderAttempt(
                provider="mineru",
                status="success",
                duration_ms=_duration_ms(started),
                raw_text_length=len(mineru_text),
                pages=mineru_result.get("pages"),
            ),
            on_attempt,
        )
        evidence.raw_text = mineru_text
        evidence.extraction_method = "mineru"
        evidence.structured_document = mineru_result.get("structured_document")
        evidence.pages = mineru_result.get("pages")
        return evidence.model_dump(mode="json")
    if mineru_result:
        _add_evidence_attempt(
            evidence,
            EvidenceProviderAttempt(
                provider="mineru",
                status="skipped",
                duration_ms=_duration_ms(started),
                error_message="MinerU returned no text evidence.",
            ),
            on_attempt,
        )

    # Neither Google Drive OCR nor MinerU produced usable text.
    evidence.raw_text = raw_text
    evidence.extraction_method = ocr_result.get("provider") or "google_drive_ocr"
    evidence.ocr_used = True
    evidence.pages = ocr_result.get("pages")
    if ocr_error:
        evidence.error = ocr_error
    elif source_type == "image":
        # Both providers came back genuinely empty (not an exception).
        # Evidence still counts as "non-empty" because the source image
        # exists, so parsing continues on vision input alone - but that is a
        # real quality degradation the operator should see, not a silent
        # normal run.
        evidence.consistency_warnings.append(
            "Google Drive OCR и MinerU не вернули текст; распознавание "
            "опирается только на изображение без OCR-текста."
        )
    return evidence.model_dump(mode="json")


def _extract_pdf_text(file_path: str) -> str:
    try:
        from pypdf import PdfReader
    except ImportError:
        return ""
    try:
        reader = PdfReader(file_path)
        return "\n\n".join((page.extract_text() or "").strip() for page in reader.pages).strip()
    except Exception:  # noqa: BLE001 - malformed/scanned PDFs continue to MinerU/OCR
        return ""


def _run_mineru_with_health_guard(
    file_path: str,
    fallback_filename: str | None,
    *,
    pipeline_logs: list[dict[str, Any]] | None = None,
    on_log: Any | None = None,
) -> tuple[dict[str, Any] | None, str | None]:
    health = mineru_health()
    health_reason = str(health.get("reason") or "")
    cache_incomplete = "model cache is incomplete" in health_reason.lower()
    if not health.get("ready") and cache_incomplete:
        if pipeline_logs is not None:
            _append_pipeline_log(
                pipeline_logs,
                _pipeline_log(
                    "mineru_skipped_unhealthy",
                    "warning",
                    "MinerU пропущен: локальный кэш моделей не готов.",
                    error=health_reason,
                ),
                on_log=on_log,
            )
        return None, health_reason

    try:
        mineru_result = _extract_with_mineru(file_path, fallback_filename)
    except Exception as exc:  # noqa: BLE001 - OCR remains the fallback path
        error_message = str(exc)
        if pipeline_logs is not None:
            _append_pipeline_log(
                pipeline_logs,
                _pipeline_log(
                    "mineru_failed",
                    "error",
                    "MinerU extraction завершился ошибкой.",
                    error=error_message,
                ),
                on_log=on_log,
            )
        return None, error_message

    if pipeline_logs is not None:
        _append_pipeline_log(
            pipeline_logs,
            _pipeline_log(
                "mineru_complete" if _payload_has_useful_data(mineru_result.get("payload") or {}) else "mineru_empty",
                "ok" if _payload_has_useful_data(mineru_result.get("payload") or {}) else "warning",
                "MinerU extraction завершен."
                if _payload_has_useful_data(mineru_result.get("payload") or {})
                else "MinerU extraction не дал полезного payload.",
                provider=mineru_result.get("provider"),
                raw_text_length=len(mineru_result.get("raw_text") or ""),
                items_count=len((mineru_result.get("payload") or {}).get("items") or []),
                pages=mineru_result.get("pages"),
            ),
            on_log=on_log,
        )
    return mineru_result, None


def _source_type(file_path: str, fallback_filename: str | None) -> str:
    suffix = Path(fallback_filename or file_path).suffix.lower()
    if suffix == ".pdf":
        return "pdf"
    if suffix in {".jpg", ".jpeg", ".png", ".webp", ".tif", ".tiff", ".heic"}:
        return "image"
    if suffix in {".xls", ".xlsx", ".csv"}:
        return "excel"
    if suffix == ".xml":
        return "xml"
    return "unknown"


def _extract_with_ocr(file_path: str, fallback_filename: str | None) -> dict[str, Any]:
    try:
        ocr_result = recognize_invoice_image(file_path)
    except OcrConfigurationError as exc:
        return {
            "provider": "manual_review_fallback",
            "raw_text": "",
            "confidence": None,
            "pages": 0,
            "error": str(exc),
            "payload": extract_invoice_payload_with_fallback("", fallback_filename),
        }
    except OcrProviderError as exc:
        return {
            "provider": exc.provider,
            "raw_text": "",
            "confidence": None,
            "pages": 0,
            "error": str(exc),
            "error_type": type(exc).__name__,
            "provider_operation": exc.operation,
            "provider_attempts": exc.attempts,
            "provider_retryable": exc.retryable,
            "payload": extract_invoice_payload_with_fallback("", fallback_filename),
        }
    except Exception as exc:  # noqa: BLE001 - external OCR failures must not break upload
        return {
            "provider": "manual_review_fallback",
            "raw_text": "",
            "confidence": None,
            "pages": 0,
            "error": str(exc),
            "payload": extract_invoice_payload_with_fallback("", fallback_filename),
        }

    payload = extract_invoice_payload_with_fallback(ocr_result.get("raw_text") or "", fallback_filename)
    return {
        "provider": ocr_result.get("provider") or "google_drive_ocr",
        "raw_text": ocr_result.get("raw_text") or "",
        "confidence": ocr_result.get("confidence"),
        "pages": ocr_result.get("pages"),
        "temporary_document_id": ocr_result.get("temporary_document_id"),
        "payload": payload,
        "parser_provider": payload.get("parser_provider"),
        "parser_notes": payload.get("parser_notes", []),
    }


def _manual_review_result(error_message: str | None, fallback_filename: str | None) -> dict[str, Any]:
    return {
        "provider": "manual_review_fallback",
        "raw_text": "",
        "confidence": None,
        "pages": 0,
        "error": error_message,
        "payload": extract_invoice_payload_with_fallback("", fallback_filename),
    }


def _extract_with_mineru(file_path: str, fallback_filename: str | None) -> dict[str, Any]:
    raw_result = _run_mineru_command(file_path)
    mineru_payload = _normalize_mineru_payload(raw_result, fallback_filename)
    fallback_payload = extract_invoice_payload_with_fallback(
        mineru_payload.get("raw_text") or "",
        fallback_filename,
    )
    payload = _merge_payloads(mineru_payload, fallback_payload)
    payload["parser_provider"] = "mineru"
    notes = list(payload.get("parser_notes", []))
    notes.append("MinerU used as the primary document extraction backend.")
    payload["parser_notes"] = notes
    return {
        "provider": "mineru",
        "raw_text": mineru_payload.get("raw_text") or "",
        "confidence": mineru_payload.get("confidence"),
        "pages": mineru_payload.get("pages"),
        "payload": payload,
        "parser_provider": payload.get("parser_provider"),
        "parser_notes": payload.get("parser_notes", []),
        "structured_document": raw_result if isinstance(raw_result, dict) else None,
    }


def _run_mineru_command(file_path: str) -> Any:
    with tempfile.TemporaryDirectory(prefix="mineru-") as output_dir:
        command = _build_mineru_command(file_path, output_dir)
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=settings.mineru_timeout_seconds,
            check=False,
        )
        if completed.returncode != 0:
            stderr = (completed.stderr or "").strip()
            raise DocumentExtractionError(
                f"MinerU command failed with exit code {completed.returncode}: {stderr or 'no stderr output'}"
            )
        structured_output = _read_mineru_output(Path(output_dir))
        stdout = (completed.stdout or "").strip()
        if structured_output is not None:
            if stdout and isinstance(structured_output, dict):
                structured_output = {**structured_output, "_stdout": stdout}
            return structured_output
        if not stdout:
            raise DocumentExtractionError("MinerU command returned no output and no output files were found.")
        try:
            return json.loads(stdout)
        except json.JSONDecodeError:
            return stdout


def _build_mineru_command(file_path: str, output_dir: str) -> list[str]:
    command_template = settings.mineru_command or DEFAULT_MINERU_COMMAND
    quoted_file_path = shlex.quote(file_path)
    command_text = command_template.format(
        python_executable=shlex.quote(sys.executable),
        file_path=quoted_file_path,
        file=quoted_file_path,
        output_dir=shlex.quote(output_dir),
    )
    return shlex.split(command_text)


def _read_mineru_output(output_dir: Path) -> Any:
    if not output_dir.exists():
        return None

    json_candidates = sorted(
        [path for path in output_dir.rglob("*.json") if path.is_file()],
        key=lambda path: (len(path.parts), len(path.name), str(path)),
    )
    parsed_json: list[tuple[Path, Any]] = []
    for candidate in json_candidates:
        try:
            value = json.loads(candidate.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        parsed_json.append((candidate, value))
        if isinstance(value, dict) and any(
            value.get(key) not in (None, "", [])
            for key in ("raw_text", "markdown", "text", "content", "items", "document")
        ):
            return value

    markdown_candidates = sorted(
        [path for path in output_dir.rglob("*.md") if path.is_file()]
        + [path for path in output_dir.rglob("*.markdown") if path.is_file()]
        + [path for path in output_dir.rglob("*.txt") if path.is_file()],
        key=lambda path: (len(path.parts), len(path.name), str(path)),
    )
    texts = []
    for candidate in markdown_candidates:
        try:
            text = candidate.read_text(encoding="utf-8").strip()
        except OSError:
            continue
        if text:
            texts.append(text)
    markdown = "\n\n".join(texts)

    for candidate, value in parsed_json:
        if candidate.name.endswith("_content_list.json") and isinstance(value, list):
            pages = {
                item.get("page_idx")
                for item in value
                if isinstance(item, dict) and item.get("page_idx") is not None
            }
            return {
                "markdown": markdown,
                "content_list": value,
                "pages": len(pages) or None,
            }

    if markdown:
        return markdown
    if parsed_json:
        return parsed_json[0][1]
    return None


def _normalize_mineru_payload(raw_result: Any, fallback_filename: str | None) -> dict[str, Any]:
    if isinstance(raw_result, dict):
        raw_text = (
            raw_result.get("raw_text")
            or raw_result.get("markdown")
            or raw_result.get("text")
            or raw_result.get("content")
            or ""
        )
        pages = raw_result.get("pages")
        if isinstance(pages, list):
            pages = len(pages)
        confidence = raw_result.get("confidence")
        header_source = raw_result.get("document") or raw_result.get("header") or raw_result
        structured_fields = _extract_mineru_content_list_fields(raw_result.get("content_list"))
        payload = extract_invoice_payload_with_fallback(str(raw_text or ""), fallback_filename)
        mapped = {
            **payload,
            "supplier": structured_fields.get("supplier") or _first_non_empty(header_source, "supplier", "seller", "supplier_name") or payload.get("supplier"),
            "supplier_legal_name": structured_fields.get("supplier") or _first_non_empty(header_source, "supplier_legal_name", "supplier_name") or payload.get("supplier_legal_name"),
            "invoice_number": structured_fields.get("invoice_number") or _first_non_empty(header_source, "invoice_number", "document_number", "number") or payload.get("invoice_number"),
            "invoice_date": _first_non_empty(header_source, "invoice_date", "date") or payload.get("invoice_date"),
            "venue": _first_non_empty(header_source, "venue", "trade_point", "recipient") or payload.get("venue"),
            "delivery_address": _first_non_empty(header_source, "delivery_address") or payload.get("delivery_address"),
            "display_store": _first_non_empty(header_source, "display_store", "store") or payload.get("display_store"),
            "store": _first_non_empty(header_source, "store") or payload.get("store"),
            "document_form": _first_non_empty(header_source, "document_form", "form") or payload.get("document_form"),
            "supplier_inn": structured_fields.get("supplier_inn") or _first_non_empty(header_source, "supplier_inn", "inn") or payload.get("supplier_inn"),
            "shipper": _first_non_empty(header_source, "shipper", "consignor") or payload.get("shipper"),
            "consignee": _first_non_empty(header_source, "consignee") or payload.get("consignee"),
            "recipient": _first_non_empty(header_source, "recipient", "buyer") or payload.get("recipient"),
            "trade_point": _first_non_empty(header_source, "trade_point") or payload.get("trade_point"),
            "warehouse": _first_non_empty(header_source, "warehouse") or payload.get("warehouse"),
            "basis": _first_non_empty(header_source, "basis") or payload.get("basis"),
            "total_sum": structured_fields.get("total_sum") or _first_non_empty(header_source, "total_sum", "sum", "amount") or payload.get("total_sum"),
        }
        items = structured_fields.get("items") or raw_result.get("items")
        if isinstance(items, list) and items:
            mapped["items"] = [_normalize_item(item) for item in items]
        mapped["raw_text"] = str(raw_text or "")
        mapped["parser_provider"] = "mineru"
        mapped["pages"] = pages
        mapped["confidence"] = confidence
        notes = list(mapped.get("parser_notes", []))
        notes.append("MinerU returned JSON output.")
        mapped["parser_notes"] = notes
        return mapped

    raw_text = str(raw_result or "")
    payload = extract_invoice_payload_with_fallback(raw_text, fallback_filename)
    payload["parser_provider"] = "mineru"
    payload["parser_notes"] = [*payload.get("parser_notes", []), "MinerU returned plain text output."]
    return payload


def _extract_mineru_content_list_fields(content_list: Any) -> dict[str, Any]:
    if not isinstance(content_list, list):
        return {}

    text_parts: list[str] = []
    items: list[dict[str, Any]] = []
    for block in content_list:
        if not isinstance(block, dict):
            continue
        block_text = str(block.get("text") or "").strip()
        if block_text:
            text_parts.append(block_text)
        table_html = str(block.get("table_body") or "").strip()
        if not table_html:
            continue
        rows = _mineru_html_table_rows(table_html)
        text_parts.extend(cell for row in rows for cell in row if cell)
        items.extend(_extract_mineru_table_items(rows))

    joined = "\n".join(text_parts)
    supplier = None
    supplier_match = re.search(
        r'Общество\s+с\s+ограниченной\s+ответственностью\s*["«]([^"»]{2,120})["»]',
        joined,
        flags=re.IGNORECASE,
    )
    if supplier_match:
        supplier = f'ООО "{supplier_match.group(1).strip().upper()}"'

    invoice_number = None
    number_match = re.search(
        r"Универсальн\w*\s+передаточн\w*\s+документ\D{0,20}(?:№|No|N)\s*([A-Za-zА-Яа-яЁё0-9-]{2,})",
        joined,
        flags=re.IGNORECASE,
    )
    if number_match:
        invoice_number = number_match.group(1)

    supplier_inn = None
    inn_match = re.search(
        r"ИНН/КПП\s+продавца[\s\S]{0,240}?\b(\d{10}|\d{12})\s*/\s*\d{9}\b",
        joined,
        flags=re.IGNORECASE,
    )
    if inn_match:
        supplier_inn = inn_match.group(1)

    return {
        "supplier": supplier,
        "supplier_inn": supplier_inn,
        "invoice_number": invoice_number,
        "total_sum": sum((item.get("sum") or 0) for item in items) or None,
        "items": items,
    }


def _mineru_html_table_rows(table_html: str) -> list[list[str]]:
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(table_html, "html.parser")
    return [
        [" ".join(cell.stripped_strings) for cell in row.find_all(["td", "th"])]
        for row in soup.find_all("tr")
    ]


def _extract_mineru_table_items(rows: list[list[str]]) -> list[dict[str, Any]]:
    unit_aliases = {
        "кг": "кг",
        "kg": "кг",
        "kr": "кг",
        "кт": "кг",
        "шт": "шт",
        "pcs": "шт",
        "л": "л",
        "l": "л",
    }
    items: list[dict[str, Any]] = []
    for cells in rows:
        normalized_cells = [re.sub(r"\s+", " ", cell).strip() for cell in cells]
        for unit_index, raw_unit in enumerate(normalized_cells):
            unit = unit_aliases.get(raw_unit.lower().strip("."))
            if unit is None or unit_index + 3 >= len(normalized_cells):
                continue
            quantity = _first_decimal(normalized_cells[unit_index + 1])
            price = _first_decimal(normalized_cells[unit_index + 2])
            if quantity is None or price is None or quantity <= 0 or price <= 0:
                continue
            expected_sum = round(quantity * price, 2)
            amounts = _all_decimals(normalized_cells[unit_index + 3])
            line_sum = next(
                (
                    value
                    for value in amounts
                    if abs(value - expected_sum) <= max(0.05, expected_sum * 0.01)
                ),
                None,
            )
            if line_sum is None:
                continue
            name = _mineru_product_name(normalized_cells[:unit_index])
            if not name:
                continue
            items.append(
                {
                    "name": name,
                    "quantity": quantity,
                    "unit": unit,
                    "price": price,
                    "sum": line_sum,
                    "vat": None,
                    "vat_percent": None,
                    "vat_sum": None,
                    "comment": None,
                    "confidence": None,
                }
            )
            break
    return items


def _mineru_product_name(cells: list[str]) -> str | None:
    for value in reversed(cells):
        candidate = re.sub(r"^(\d{1,3})(?=[A-Za-zА-Яа-яЁё])", r"\1 ", value).strip()
        candidate = re.split(r"(?i)\bвс[eе][gгr][oо]\s+к\s+оплате\b", candidate, maxsplit=1)[0]
        candidate = re.sub(r"^\d{1,3}\s+", "", candidate).strip(" -—")
        lowered = candidate.lower()
        if len(candidate) < 3 or not re.search(r"[А-Яа-яЁё]{2,}", candidate):
            continue
        if any(marker in lowered for marker in ("документ составлен", "всего к оплате", "единица измерения")):
            continue
        return candidate
    return None


def _first_decimal(value: str) -> float | None:
    values = _all_decimals(value)
    return values[0] if values else None


def _all_decimals(value: str) -> list[float]:
    numbers = []
    for match in re.finditer(r"(?<!\d)\d[\d ]*(?:[,.]\d+)?(?!\d)", value):
        normalized = match.group(0).replace(" ", "").replace(",", ".")
        try:
            numbers.append(float(normalized))
        except ValueError:
            continue
    return numbers


def _merge_payloads(primary: dict[str, Any], fallback: dict[str, Any]) -> dict[str, Any]:
    merged = dict(fallback)
    for key, value in primary.items():
        if key == "parser_notes":
            continue
        if key == "items" and value:
            merged[key] = value
            continue
        if value not in (None, "", []):
            merged[key] = value
    merged_notes = list(fallback.get("parser_notes", []))
    merged_notes.extend(primary.get("parser_notes", []))
    merged["parser_notes"] = merged_notes
    if primary.get("parser_provider"):
        merged["parser_provider"] = primary["parser_provider"]
    return merged


def _normalize_item(item: Any) -> dict[str, Any]:
    if not isinstance(item, dict):
        return {
            "name": str(item),
            "quantity": 1.0,
            "unit": "шт",
            "price": 0.0,
            "sum": None,
            "vat": None,
            "comment": None,
            "confidence": None,
        }
    return {
        "name": _first_non_empty(item, "name", "item_name", "product_name") or "",
        "quantity": _coerce_float(item.get("quantity") or item.get("qty") or 0),
        "unit": str(item.get("unit") or "шт"),
        "price": _coerce_float(item.get("price") or 0),
        "sum": _coerce_float(item.get("sum") or item.get("line_sum") or item.get("total") or 0) if item.get("sum") is not None or item.get("line_sum") is not None or item.get("total") is not None else None,
        "vat": item.get("vat"),
        "comment": item.get("comment"),
        "confidence": item.get("confidence"),
        "vat_percent": _coerce_float(item.get("vat_percent")) if item.get("vat_percent") not in (None, "") else None,
        "vat_sum": _coerce_float(item.get("vat_sum")) if item.get("vat_sum") not in (None, "") else None,
    }


def _first_non_empty(source: Any, *keys: str) -> Any:
    if not isinstance(source, dict):
        return None
    for key in keys:
        value = source.get(key)
        if value not in (None, "", []):
            return value
    return None


def _coerce_float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _payload_has_useful_data(payload: dict[str, Any]) -> bool:
    if payload.get("items"):
        return True
    for field in ("supplier", "invoice_number", "invoice_date", "venue", "document_form", "total_sum"):
        if payload.get(field) not in (None, ""):
            return True
    return False


def _payload_validation_errors(payload: dict[str, Any]) -> list[str]:
    if not _payload_has_useful_data(payload):
        return ["structured payload is empty"]
    items = payload.get("items")
    if settings.invoice_allow_header_only_documents:
        return []
    if not isinstance(items, list) or not items:
        return ["товарные строки отсутствуют"]
    if not any(
        str(item.get("name") or item.get("raw_name") or "").strip()
        for item in items
        if isinstance(item, dict)
    ):
        return ["товарные строки не содержат наименований"]
    return []


def _evidence_has_content(evidence: dict[str, Any]) -> bool:
    if (evidence.get("raw_text") or "").strip():
        return True
    structured_document = evidence.get("structured_document")
    if isinstance(structured_document, dict):
        return any(value not in (None, "", [], {}) for value in structured_document.values())
    if isinstance(structured_document, list):
        return bool(structured_document)
    for page in evidence.get("page_sources") or []:
        if not isinstance(page, dict):
            continue
        candidate = Path(page.get("prepared_path") or page.get("original_path") or "")
        if candidate.is_file():
            return True
    return False


def _duration_ms(started: float) -> int:
    return max(0, round((time.perf_counter() - started) * 1000))


def _add_evidence_attempt(
    evidence: DocumentEvidence,
    attempt: EvidenceProviderAttempt,
    on_attempt: Any | None,
) -> None:
    evidence.provider_attempts.append(attempt)
    if on_attempt is not None:
        on_attempt(attempt.model_dump(mode="json"))


def _provider_attempt_log(attempt: dict[str, Any]) -> dict[str, Any]:
    provider = attempt["provider"]
    status = attempt["status"]
    details = {key: value for key, value in attempt.items() if key != "status"}
    return _pipeline_log(
        f"evidence_provider_{provider}_{'start' if status == 'running' else 'complete'}",
        (
            "running"
            if status == "running"
            else ("ok" if status == "success" else ("warning" if status == "skipped" else "error"))
        ),
        f"Evidence provider {provider}: {status}.",
        **details,
    )


def _notify_evidence_attempt_start(provider: str, on_attempt: Any | None) -> None:
    if on_attempt is not None:
        on_attempt({"provider": provider, "status": "running"})


def _pipeline_log(
    stage: str,
    status: str,
    message: str,
    *,
    recommendation: str | None = None,
    **details: Any,
) -> dict[str, Any]:
    log = {
        "stage": stage,
        "status": status,
        "message": message,
        "details": details,
    }
    if recommendation:
        log["recommendation"] = recommendation
    return log


def _append_pipeline_log(
    pipeline_logs: list[dict[str, Any]],
    log: dict[str, Any],
    *,
    on_log: Any | None = None,
) -> None:
    pipeline_logs.append(log)
    if on_log is not None:
        on_log(log)
