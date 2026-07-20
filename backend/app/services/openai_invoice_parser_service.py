import base64
import json
import logging
import mimetypes
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from app.config import settings
from app.schemas.invoice_parser import InvoiceParserResult, InvoiceReviewFlag, InvoiceSourceTrace
from app.services.invoice_normalization_service import normalize_invoice_result, to_legacy_invoice_payload


logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """Ты извлекаешь данные российских накладных из предоставленного evidence.
Возвращай только значения, подтвержденные документом. Не придумывай номера,
суммы, единицы, названия и отсутствующие строки. Сохраняй исходную формулировку
товара в raw_name, а подтверждающий фрагмент документа в source_fragment.
Добавляй review_flags для неоднозначных, нечитаемых или отсутствующих значений,
расхождений итогов и неуверенных границ таблицы.

Для фотографий анализируй каждую переданную страницу, даже если OCR-текст
неполный или противоречит изображению. Учитывай возможный поворот страницы.
Номер документа бери только из реквизита номера документа, не из артикула,
штрихкода или внутреннего кода товарной строки. Не объединяй соседние товары в
одну строку. Повторяющиеся строки чека сохраняй отдельными строками в исходном
порядке; агрегацию backend выполнит отдельно, если она потребуется.

Для каждой товарной строки дополнительно выполни структурный разбор:
- clean_name: название без номера строки, служебных слов ТОВАР/ПОЗИЦИЯ/АРТИКУЛ,
  внутренних кодов, лишних скобок и технической единицы в начале строки;
- normalized_name_candidate: короткое читаемое базовое название для последующего
  deterministic-сопоставления со справочником;
- brand_or_descriptor: бренд, сорт, жирность, вкус, размер, тип упаковки,
  назначение и другие смысловые уточнения;
- package: фасовка, объем, вес или размер одной базовой единицы товара с
  исходным написанием в package.raw;
- document_unit и quantity_document: единица и количество непосредственно из
  документа; они должны совпадать с unit и quantity;
- units_per_package: значение из отдельного столбца документа «Состав ед. изм.»
  (или аналогичного столбца количества единиц в упаковке/коробе). Извлекай его
  только когда оно явно указано в товарной строке; не выводи это поле из названия
  товара. Если столбец пустой или отсутствует, оставляй null;
- codes: удаленные из названия внутренние коды, включая N+13968, M+13649, +19497;
- quantity_multiplier, accounting_quantity_candidate и accounting_unit_candidate:
  только кандидаты для последующей проверки backend-кодом.

Не удаляй из названия смысловые слова. Распознавай 800Г, 0.8КГ, 1Л, 500МЛ,
10ШТ, 20 ПАК, размеры 30*40СМ и составные фасовки 12Х1Л. Для пересчета используй:
г -> кг: value / 1000; кг -> кг: value; мл -> л: value / 1000;
л -> л: value; шт -> шт: value. Для весового товара в КГ и штучного товара без
фасовки multiplier=1. Для 0,5Л 12ШТ multiplier=6 и единица учета л. Если в отдельном столбце
«Состав ед. изм.» указано 6, а quantity_document=2 УП и package=1,5Л, верни
units_per_package=6; итоговый коэффициент backend рассчитает как 6 * 1,5 = 9 л
на одну УП, а итоговое количество в УС как 2 * 9 = 18 л.

Если товар, фасовку или единицу нельзя определить уверенно, установи
needs_review=true, confidence<0.8 и кратко объясни причину в review_reason.

Ориентиры:
- «3 ТОВАР : ШТ. [N+13968 КЕФИР ФЕРМЕРСКИЙ 800Г» ->
  clean_name «КЕФИР ФЕРМЕРСКИЙ», package 800 г, multiplier 0.8 кг,
  codes [«N+13968»];
- «ВОДА ПИТЬЕВАЯ 0,5Л 12ШТ» -> package.raw «0,5Л»,
  multiplier 6, accounting_unit_candidate «л»;
- «ПАКЕТ-МАЙКА ВИКТОРИЯ 65*40СМ» -> package.value null,
  package.unit «см», package.raw «65*40СМ», multiplier 1 шт;
- «САЛФЕТКИ БУМ 24Х24 100Л» -> needs_review=true, потому что «Л» может
  означать листы, а не литры.

Все поля нормализации являются кандидатами: окончательное сопоставление с
листами «Товары» и «Справочник фасовок», расчеты, статусы и запись в Google
Sheets выполняет backend. Ты не выбираешь колонки и не пишешь во внешние системы."""


class OpenAIInvoiceParserError(RuntimeError):
    pass


def parse_invoice_with_openai(
    evidence: dict[str, Any],
    *,
    client: Any | None = None,
) -> dict[str, Any]:
    api_client = client or _create_client()
    request_payload = _build_evidence_payload(evidence)
    request_input = _build_openai_input(evidence, request_payload)
    try:
        response = api_client.responses.parse(
            model=settings.openai_invoice_model,
            instructions=SYSTEM_PROMPT,
            input=request_input,
            text_format=InvoiceParserResult,
        )
    except Exception as exc:  # noqa: BLE001 - provider failures are pipeline errors
        _write_debug_log(evidence, None, None, error=str(exc))
        raise OpenAIInvoiceParserError(f"OpenAI invoice parsing failed: {exc}") from exc

    parsed = getattr(response, "output_parsed", None)
    if parsed is None:
        raise OpenAIInvoiceParserError("OpenAI returned no structured invoice payload.")
    validated = parsed if isinstance(parsed, InvoiceParserResult) else InvoiceParserResult.model_validate(parsed)
    validated.source_trace = _source_trace(evidence, validated.source_trace)
    for warning in evidence.get("consistency_warnings") or []:
        validated.review_flags.append(
            InvoiceReviewFlag(
                scope="document",
                field="page_consistency",
                reason=str(warning),
                severity="warning",
            )
        )
    normalized = normalize_invoice_result(
        validated,
        ocr_error=None if _evidence_has_image_pages(evidence) else evidence.get("error"),
    )
    payload = to_legacy_invoice_payload(normalized)
    _write_debug_log(evidence, validated, normalized)
    return payload


def _create_client() -> Any:
    if not settings.openai_api_key:
        raise OpenAIInvoiceParserError("OPENAI_API_KEY is not configured.")
    try:
        from openai import OpenAI
    except ImportError as exc:
        raise OpenAIInvoiceParserError("OpenAI SDK is not installed.") from exc
    return OpenAI(api_key=settings.openai_api_key, timeout=settings.openai_timeout_seconds)


def _build_evidence_payload(evidence: dict[str, Any]) -> dict[str, Any]:
    return {
        "evidence_version": evidence.get("evidence_version", "legacy"),
        "logical_document_id": evidence.get("logical_document_id"),
        "filename": evidence.get("filename"),
        "source_type": evidence.get("source_type", "unknown"),
        "ocr_used": bool(evidence.get("ocr_used")),
        "extraction_method": evidence.get("extraction_method", ""),
        "pages": evidence.get("pages"),
        "page_sources": [
            {
                "page_number": page.get("page_number"),
                "filename": page.get("filename"),
                "source_type": page.get("source_type"),
                "transformations": page.get("transformations") or [],
                "quality": page.get("quality") or {},
            }
            for page in (evidence.get("page_sources") or [])
            if isinstance(page, dict)
        ],
        "provider_attempts": evidence.get("provider_attempts") or [],
        "evidence_errors": evidence.get("errors") or [],
        "consistency_warnings": evidence.get("consistency_warnings") or [],
        "raw_text": (evidence.get("raw_text") or "")[: settings.openai_max_evidence_chars],
        "structured_document": evidence.get("structured_document"),
    }


def _source_trace(evidence: dict[str, Any], model_trace: InvoiceSourceTrace) -> InvoiceSourceTrace:
    raw_text = evidence.get("raw_text") or ""
    return InvoiceSourceTrace(
        source_type=evidence.get("source_type") or model_trace.source_type,
        ocr_used=bool(evidence.get("ocr_used")),
        extraction_method=evidence.get("extraction_method") or model_trace.extraction_method,
        raw_text_sample=raw_text[:500],
    )


def _build_openai_input(
    evidence: dict[str, Any],
    request_payload: dict[str, Any],
) -> list[dict[str, Any]]:
    content: list[dict[str, Any]] = [
        {
            "type": "input_text",
            "text": json.dumps(request_payload, ensure_ascii=False),
        }
    ]
    image_count = 0
    for page in evidence.get("page_sources") or []:
        if not isinstance(page, dict) or page.get("source_type") != "image":
            continue
        if image_count >= max(0, settings.openai_max_image_pages):
            break
        image_path = Path(page.get("prepared_path") or page.get("original_path") or "")
        if not image_path.is_file():
            continue
        size = image_path.stat().st_size
        if size <= 0 or size > settings.openai_max_image_bytes:
            continue
        mime_type = mimetypes.guess_type(image_path.name)[0] or "image/jpeg"
        encoded = base64.b64encode(image_path.read_bytes()).decode("ascii")
        content.append(
            {
                "type": "input_text",
                "text": f"Страница {page.get('page_number') or image_count + 1}: {page.get('filename') or image_path.name}",
            }
        )
        content.append(
            {
                "type": "input_image",
                "image_url": f"data:{mime_type};base64,{encoded}",
                "detail": settings.openai_image_detail,
            }
        )
        image_count += 1
    return [{"role": "user", "content": content}]


def _evidence_has_image_pages(evidence: dict[str, Any]) -> bool:
    return any(
        isinstance(page, dict)
        and page.get("source_type") == "image"
        and Path(page.get("prepared_path") or page.get("original_path") or "").is_file()
        for page in (evidence.get("page_sources") or [])
    )


def _write_debug_log(
    evidence: dict[str, Any],
    parsed: InvoiceParserResult | None,
    normalized: Any | None,
    *,
    error: str | None = None,
) -> None:
    record = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "evidence": _build_evidence_payload(evidence),
        "model_json": parsed.model_dump(mode="json") if parsed else None,
        "normalized_json": normalized.model_dump(mode="json") if normalized else None,
        "review_flags": normalized.model_dump(mode="json").get("review_flags", []) if normalized else [],
        "error": error,
    }
    logger.info("invoice_parser_trace=%s", json.dumps(record, ensure_ascii=False))
    if not settings.openai_debug_log_enabled:
        return
    target_dir = Path(settings.openai_debug_log_dir)
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / f"invoice-{datetime.now(timezone.utc):%Y%m%dT%H%M%S}-{uuid4().hex[:8]}.json"
    target.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
