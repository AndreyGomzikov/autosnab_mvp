import re
from datetime import date, datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any, Literal

from app.schemas.invoice_parser import (
    InvoiceParserResult,
    InvoiceReviewFlag,
    NormalizedInvoiceResult,
)
from app.services.item_normalization_service import normalize_item_candidate
from app.services.normalization import canonical_invoice_number


MONEY_TOLERANCE = Decimal("0.02")


def normalize_invoice_result(
    parsed: InvoiceParserResult | dict[str, Any],
    *,
    duplicate: Literal["", "Да", "?"] = "",
    ocr_error: str | None = None,
) -> NormalizedInvoiceResult:
    source = parsed if isinstance(parsed, InvoiceParserResult) else InvoiceParserResult.model_validate(parsed)
    result = NormalizedInvoiceResult(**source.model_dump())
    result.duplicate = duplicate

    result.document.document_date = _normalize_date(
        result.document.document_date,
        result.review_flags,
        result.normalization_log,
    )
    result.document.document_number = _clean_text(result.document.document_number)
    result.document.document_form = _clean_text(result.document.document_form)
    result.document.shipper = _clean_text(result.document.shipper)
    result.document.receiver = _clean_text(result.document.receiver)
    result.document.basis = _normalize_basis(
        result.document.basis,
        result.document.document_form,
        result.document.document_number,
        result.document.document_date,
        result.review_flags,
        result.normalization_log,
    )
    result.document.supplier_inn = _normalize_inn(
        result.document.supplier_inn,
        result.review_flags,
        result.normalization_log,
    )

    document_money_fields = ("total_without_vat", "vat_total", "total_with_vat")
    for field in document_money_fields:
        setattr(result.document, field, _money(getattr(result.document, field)))

    for position, item in enumerate(result.items, start=1):
        line_number = item.line_number or position
        item.line_number = line_number
        item.raw_name = _clean_text(item.raw_name)
        item.unit = _clean_text(item.unit)
        item.quantity = _number(item.quantity)
        item.price = _money(item.price)
        item.amount_without_vat = _money(item.amount_without_vat)
        item.vat_amount = _money(item.vat_amount)
        item.amount_with_vat = _money(item.amount_with_vat)
        item.vat_rate = _normalize_vat_rate(item.vat_rate)
        for issue in normalize_item_candidate(item):
            _flag(
                result,
                "item",
                line_number,
                issue["field"],
                issue["reason"],
                issue["severity"],
            )

        if not item.raw_name:
            _flag(result, "item", line_number, "raw_name", "Не распознано наименование товара.", "error")
        if item.quantity is None or item.quantity <= 0:
            _flag(result, "item", line_number, "quantity", "Количество отсутствует или не больше нуля.", "error")
        if item.price is None or item.price < 0:
            _flag(result, "item", line_number, "price", "Цена отсутствует или отрицательна.", "error")

        calculated = _multiply(item.quantity, item.price)
        if item.amount_without_vat is None and calculated is not None:
            item.amount_without_vat = calculated
            result.normalization_log.append(f"items[{line_number}].amount_without_vat calculated")
        elif calculated is not None and not _close(item.amount_without_vat, calculated):
            _flag(
                result,
                "item",
                line_number,
                "amount_without_vat",
                "Стоимость строки не совпадает с количеством, умноженным на цену.",
                "warning",
            )

        calculated_with_vat = _add(item.amount_without_vat, item.vat_amount)
        if item.amount_with_vat is None and calculated_with_vat is not None:
            item.amount_with_vat = calculated_with_vat
            result.normalization_log.append(f"items[{line_number}].amount_with_vat calculated")
        elif calculated_with_vat is not None and not _close(item.amount_with_vat, calculated_with_vat):
            _flag(
                result,
                "item",
                line_number,
                "amount_with_vat",
                "Итог строки не совпадает со стоимостью и суммой НДС.",
                "warning",
            )

    if not result.items:
        _flag(result, "document", None, "items", "В документе не распознаны товарные строки.", "error")

    item_total = _sum(item.amount_with_vat for item in result.items)
    if result.document.total_with_vat is None and item_total is not None:
        result.document.total_with_vat = item_total
        result.normalization_log.append("document.total_with_vat calculated from items")
    elif item_total is not None and not _close(result.document.total_with_vat, item_total):
        _flag(
            result,
            "document",
            None,
            "total_with_vat",
            "Сумма товарных строк не совпадает с итогом документа.",
            "warning",
        )

    if ocr_error:
        _flag(result, "document", None, "source_trace", f"Ошибка OCR: {ocr_error}", "error")
        result.upload_status = "Не готово"
        result.row_status = "Ошибка загрузки"
        for item in result.items or [None]:
            result.item_corrections[(item.line_number if item else 1) or 1] = "Ошибка OCR"
    elif duplicate == "Да":
        result.upload_status = "Не готово"
        result.row_status = "Распознано"
    elif duplicate == "?":
        result.upload_status = "Требует проверки"
        result.row_status = "Распознано"
    elif result.review_flags:
        result.upload_status = "Требует проверки"
        result.row_status = "Правка вручную"
        _assign_corrections(result)

    _apply_receipt_defaults(result)

    return result


def to_legacy_invoice_payload(result: NormalizedInvoiceResult) -> dict[str, Any]:
    document = result.document
    return {
        "supplier": document.supplier_name or None,
        "supplier_legal_name": document.supplier_name or None,
        "invoice_number": document.document_number or None,
        "invoice_date": document.document_date or None,
        "document_form": document.document_form or None,
        "supplier_inn": document.supplier_inn or None,
        "shipper": document.shipper or None,
        "consignee": document.receiver or None,
        "recipient": document.receiver or None,
        "basis": document.basis or None,
        "total_sum": document.total_with_vat,
        "items": [
            {
                "name": item.raw_name,
                "raw_name": item.raw_name,
                "clean_name": item.clean_name,
                "normalized_name_candidate": item.normalized_name_candidate,
                "brand_or_descriptor": item.brand_or_descriptor,
                "package": item.package.model_dump(mode="json"),
                "document_unit": item.document_unit,
                "quantity_document": item.quantity_document,
                "units_per_package": item.units_per_package,
                "quantity_multiplier": item.quantity_multiplier,
                "accounting_quantity_candidate": item.accounting_quantity_candidate,
                "accounting_unit_candidate": item.accounting_unit_candidate,
                "codes": item.codes,
                "quantity": item.quantity or 0,
                "unit": item.unit or "шт",
                "price": item.price or 0,
                "sum": item.amount_without_vat,
                "vat": item.vat_rate or None,
                "vat_percent": _vat_number(item.vat_rate),
                "vat_sum": item.vat_amount,
                "comment": item.source_fragment or None,
                "confidence": item.confidence,
                "needs_review": item.needs_review,
                "review_reason": item.review_reason,
                "line_number": item.line_number,
                "amount_with_vat": item.amount_with_vat,
                "correction": result.item_corrections.get(item.line_number or 0, ""),
            }
            for item in result.items
        ],
        "parser_provider": "openai",
        "parser_notes": [flag.reason for flag in result.review_flags],
        "parser_metadata": result.model_dump(mode="json"),
    }


def _normalize_date(
    value: str,
    flags: list[InvoiceReviewFlag],
    changes: list[str],
) -> str:
    cleaned = _clean_text(value)
    if not cleaned:
        flags.append(InvoiceReviewFlag(scope="document", field="document_date", reason="Дата документа не распознана.", severity="error"))
        return ""
    for fmt in (
        "%Y-%m-%d",
        "%d.%m.%Y",
        "%d/%m/%Y",
        "%d-%m-%Y",
        "%d.%m.%y",
        "%d.%m.%y %H:%M",
    ):
        try:
            normalized = datetime.strptime(cleaned, fmt).date().isoformat()
            if normalized != cleaned:
                changes.append("document.document_date normalized")
            return normalized
        except ValueError:
            continue
    russian_date = _normalize_russian_date(cleaned)
    if russian_date:
        changes.append("document.document_date normalized")
        return russian_date
    flags.append(InvoiceReviewFlag(scope="document", field="document_date", reason="Дата документа имеет неизвестный формат.", severity="error"))
    return cleaned


def _normalize_basis(
    value: str,
    document_form: str,
    document_number: str,
    document_date: str,
    flags: list[InvoiceReviewFlag],
    changes: list[str],
) -> str:
    cleaned = _clean_text(value)
    if not cleaned:
        return ""
    normalized = re.sub(r"\s+", " ", cleaned).strip(" ,;")
    lowered = normalized.lower()
    form_lower = (document_form or "").strip().lower()
    if "универсальный передаточный документ" in lowered or "товарная накладная" in lowered:
        if form_lower and (form_lower in lowered or "документ" in lowered or "наклад" in lowered):
            flags.append(
                InvoiceReviewFlag(
                    scope="document",
                    field="basis",
                    reason="Основание совпадает с названием формы документа и очищено.",
                    severity="warning",
                )
            )
            changes.append("document.basis cleared as document-form echo")
            return ""
    canonical_basis = canonical_invoice_number(normalized, document_form=document_form)
    canonical_number = canonical_invoice_number(document_number, document_form=document_form)
    if canonical_number and canonical_basis and canonical_basis == canonical_number:
        flags.append(
            InvoiceReviewFlag(
                scope="document",
                field="basis",
                reason="Основание совпадает только с номером документа и очищено.",
                severity="warning",
            )
        )
        changes.append("document.basis cleared as document-number echo")
        return ""
    if document_date and normalized.endswith(document_date):
        form_tokens = {"упд", "торг-12", "чек"}
        if form_lower in form_tokens or any(token in lowered for token in ("документ", "наклад", "чек")):
            changes.append("document.basis cleared as dated document-form echo")
            return ""
    return normalized


def _normalize_russian_date(value: str) -> str | None:
    months = {
        "января": 1,
        "февраля": 2,
        "марта": 3,
        "апреля": 4,
        "мая": 5,
        "июня": 6,
        "июля": 7,
        "августа": 8,
        "сентября": 9,
        "октября": 10,
        "ноября": 11,
        "декабря": 12,
    }
    match = re.search(
        r"\b(\d{1,2})\s+([а-яё]+)\s+(\d{4})(?:\s*(?:года|г\.?))?\b",
        value.lower(),
    )
    if not match:
        return None
    month = months.get(match.group(2))
    if month is None:
        return None
    try:
        return date(int(match.group(3)), month, int(match.group(1))).isoformat()
    except ValueError:
        return None


def _normalize_inn(
    value: str,
    flags: list[InvoiceReviewFlag],
    changes: list[str],
) -> str:
    original = str(value or "")
    cleaned = normalize_supplier_inn_value(original)
    if cleaned != original:
        changes.append("document.supplier_inn normalized")
    if len(cleaned) not in {10, 12}:
        flags.append(InvoiceReviewFlag(scope="document", field="supplier_inn", reason="ИНН поставщика должен содержать 10 или 12 цифр.", severity="warning"))
    elif not _is_valid_inn_checksum(cleaned):
        flags.append(
            InvoiceReviewFlag(
                scope="document",
                field="supplier_inn",
                reason="Контрольная сумма ИНН поставщика не совпадает.",
                severity="warning",
            )
        )
    return cleaned


def normalize_supplier_inn_value(value: Any) -> str:
    original = str(value or "")
    if not original:
        return ""

    # Common OCR shape: "ИНН/КПП" or merged INN+KPP.
    split_candidates = [re.sub(r"\D", "", part) for part in re.split(r"[/|]", original) if part]
    for candidate in split_candidates:
        if len(candidate) in {10, 12} and _is_valid_inn(candidate):
            return candidate

    digits = re.sub(r"\D", "", original)
    if len(digits) in {10, 12} and _is_valid_inn(digits):
        return digits

    if len(digits) in {19, 21}:
        prefix_length = 10 if len(digits) == 19 else 12
        prefix = digits[:prefix_length]
        if _is_valid_inn(prefix):
            return prefix

    for length in (10, 12):
        for start in range(0, max(len(digits) - length + 1, 0)):
            candidate = digits[start : start + length]
            if _is_valid_inn(candidate):
                return candidate

    if len(digits) >= 10:
        return digits[:10]
    return digits


def _is_valid_inn_checksum(value: str) -> bool:
    if not value.isdigit():
        return False
    digits = [int(digit) for digit in value]
    if len(digits) == 10:
        weights = [2, 4, 10, 3, 5, 9, 4, 6, 8]
        checksum = sum(digit * weight for digit, weight in zip(digits[:9], weights, strict=True)) % 11 % 10
        return checksum == digits[9]
    if len(digits) == 12:
        first_weights = [7, 2, 4, 10, 3, 5, 9, 4, 6, 8]
        second_weights = [3, 7, 2, 4, 10, 3, 5, 9, 4, 6, 8]
        first_checksum = (
            sum(
                digit * weight
                for digit, weight in zip(digits[:10], first_weights, strict=True)
            )
            % 11
            % 10
        )
        second_checksum = (
            sum(
                digit * weight
                for digit, weight in zip(digits[:11], second_weights, strict=True)
            )
            % 11
            % 10
        )
        return first_checksum == digits[10] and second_checksum == digits[11]
    return False


def _is_valid_inn(value: str) -> bool:
    if not value.isdigit():
        return False
    if len(value) == 10:
        checksum = sum(int(digit) * factor for digit, factor in zip(value[:9], (2, 4, 10, 3, 5, 9, 4, 6, 8), strict=False))
        return checksum % 11 % 10 == int(value[9])
    if len(value) == 12:
        checksum_11 = sum(int(digit) * factor for digit, factor in zip(value[:10], (7, 2, 4, 10, 3, 5, 9, 4, 6, 8), strict=False))
        checksum_12 = sum(int(digit) * factor for digit, factor in zip(value[:11], (3, 7, 2, 4, 10, 3, 5, 9, 4, 6, 8), strict=False))
        return checksum_11 % 11 % 10 == int(value[10]) and checksum_12 % 11 % 10 == int(value[11])
    return False


def _assign_corrections(result: NormalizedInvoiceResult) -> None:
    for flag in result.review_flags:
        if flag.scope != "item" or flag.line_number is None:
            continue
        reason = flag.reason.lower()
        correction = "Нет в справочнике" if "справочник" in reason else "Сопоставление" if "сопостав" in reason else "Другое"
        result.item_corrections.setdefault(flag.line_number, correction)


def _apply_receipt_defaults(result: NormalizedInvoiceResult) -> None:
    if not _looks_like_receipt(result.document.document_form, result.document.document_number):
        return
    for item in result.items:
        if not item.vat_rate:
            item.vat_rate = "Без НДС"
            result.normalization_log.append(f"items[{item.line_number or 0}].vat_rate receipt default")
        if item.vat_amount is None:
            item.vat_amount = 0.0
            result.normalization_log.append(f"items[{item.line_number or 0}].vat_amount receipt default")
        if item.amount_with_vat is None and item.amount_without_vat is not None:
            item.amount_with_vat = item.amount_without_vat
            result.normalization_log.append(f"items[{item.line_number or 0}].amount_with_vat receipt default")


def _looks_like_receipt(document_form: str, document_number: str) -> bool:
    form = (document_form or "").strip().lower()
    if "чек" in form or "receipt" in form:
        return True
    return str(document_number or "").strip().upper().startswith("ЧЕК")


def _flag(
    result: NormalizedInvoiceResult,
    scope: Literal["document", "item"],
    line_number: int | None,
    field: str,
    reason: str,
    severity: Literal["warning", "error"],
) -> None:
    candidate = InvoiceReviewFlag(
        scope=scope,
        line_number=line_number,
        field=field,
        reason=reason,
        severity=severity,
    )
    if candidate not in result.review_flags:
        result.review_flags.append(candidate)


def _clean_text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _decimal(value: Any) -> Decimal | None:
    if value in (None, ""):
        return None
    try:
        return Decimal(str(value).replace(" ", "").replace(",", "."))
    except (InvalidOperation, ValueError):
        return None


def _number(value: Any) -> float | None:
    number = _decimal(value)
    return float(number) if number is not None else None


def _money(value: Any) -> float | None:
    number = _decimal(value)
    return float(number.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)) if number is not None else None


def _multiply(left: Any, right: Any) -> float | None:
    left_decimal = _decimal(left)
    right_decimal = _decimal(right)
    if left_decimal is None or right_decimal is None:
        return None
    return float((left_decimal * right_decimal).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def _add(left: Any, right: Any) -> float | None:
    left_decimal = _decimal(left)
    if left_decimal is None:
        return None
    right_decimal = _decimal(right) or Decimal("0")
    return float((left_decimal + right_decimal).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def _sum(values: Any) -> float | None:
    decimals = [_decimal(value) for value in values]
    present = [value for value in decimals if value is not None]
    if not present:
        return None
    return float(sum(present, Decimal("0")).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def _close(left: Any, right: Any) -> bool:
    left_decimal = _decimal(left)
    right_decimal = _decimal(right)
    return left_decimal is not None and right_decimal is not None and abs(left_decimal - right_decimal) <= MONEY_TOLERANCE


def _normalize_vat_rate(value: str) -> str:
    cleaned = _clean_text(value)
    if not cleaned:
        return ""
    if cleaned.lower() in {"без ндс", "none", "no vat"}:
        return "Без НДС"
    match = re.search(r"\d+(?:[.,]\d+)?", cleaned)
    return f"{match.group(0).replace(',', '.')}%" if match else cleaned


def _vat_number(value: str) -> float | None:
    match = re.search(r"\d+(?:[.,]\d+)?", value or "")
    return float(match.group(0).replace(",", ".")) if match else None
