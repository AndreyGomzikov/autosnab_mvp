from typing import Any

from app.services.ocr_service import parse_invoice_text_to_payload


USEFUL_HEADER_FIELDS = (
    "supplier",
    "supplier_legal_name",
    "invoice_number",
    "invoice_date",
    "venue",
    "delivery_address",
    "display_store",
    "store",
    "document_form",
    "supplier_inn",
    "consignee",
    "recipient",
    "trade_point",
    "warehouse",
    "basis",
    "total_sum",
)


def extract_invoice_payload_with_fallback(raw_text: str, fallback_filename: str | None = None) -> dict:
    """Parse OCR text with the built-in deterministic parser.

    The parser intentionally does not call external AI APIs. If useful fields are
    not found, the backend still creates an empty sheet for manual review.
    """
    parsed = parse_invoice_text_to_payload(raw_text, fallback_filename)
    if _payload_has_useful_data(parsed):
        parsed["parser_provider"] = "deterministic_parser"
        parsed["parser_notes"] = parsed.get("parser_notes", []) + [
            "Данные разобраны встроенным regex parser без внешних AI/API-ключей.",
            "Проверьте результат в Google Таблице перед отправкой в iiko.",
        ]
        return parsed

    return _empty_manual_review_payload(raw_text)


def _payload_has_useful_data(payload: dict[str, Any]) -> bool:
    if payload.get("items"):
        return True
    for field in USEFUL_HEADER_FIELDS:
        if payload.get(field) not in (None, ""):
            return True
    return False


def _empty_manual_review_payload(raw_text: str | None = None) -> dict:
    return {
        "supplier": None,
        "supplier_legal_name": None,
        "invoice_number": None,
        "invoice_date": None,
        "venue": None,
        "delivery_address": None,
        "store": None,
        "display_store": None,
        "document_form": None,
        "supplier_inn": None,
        "consignee": None,
        "recipient": None,
        "trade_point": None,
        "warehouse": None,
        "basis": None,
        "iiko_default_store_id": None,
        "total_sum": None,
        "raw_text": raw_text,
        "items": [],
        "parser_provider": "manual_review_empty_sheet",
        "parser_notes": [
            "Встроенный regex parser не нашел полезные поля накладной.",
            "Создана пустая Google Таблица для ручной проверки и заполнения накладной.",
        ],
    }
