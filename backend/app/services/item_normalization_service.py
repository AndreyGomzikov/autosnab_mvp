import copy
import re
from decimal import Decimal, InvalidOperation
from difflib import SequenceMatcher
from typing import Any

from app.config import settings
from app.schemas.invoice_parser import InvoiceItemPackage, InvoiceParsedItem


_CODE_RE = re.compile(r"(?<![A-ZА-ЯЁ0-9])(?:[NM]\+|\+)\d+", re.IGNORECASE)
_DIMENSION_RE = re.compile(r"\b\d+(?:[.,]\d+)?\s*[*XХ]\s*\d+(?:[.,]\d+)?\s*СМ\b", re.IGNORECASE)
_COMPOUND_PACKAGE_RE = re.compile(
    r"\b(?P<count>\d+(?:[.,]\d+)?)\s*[XХ]\s*(?P<value>\d+(?:[.,]\d+)?)\s*"
    r"(?P<unit>КГ|Г|МЛ|Л|ШТ|ПАК)\b",
    re.IGNORECASE,
)
_PACKAGE_RE = re.compile(
    r"\b(?P<value>\d+(?:[.,]\d+)?)\s*(?P<unit>КГ|Г|МЛ|Л|ШТ|ПАК)\b",
    re.IGNORECASE,
)
_TECHNICAL_PREFIX_RE = re.compile(
    r"^\s*(?:\d+\s+)?(?:(?:ТОВАР|ПОЗИЦИЯ|АРТИКУЛ)\s*[:.-]?\s*)*"
    r"(?:(?:ШТ|КГ|Л|УПАК)\.?\s*[:.-]?\s*)*",
    re.IGNORECASE,
)
_PROMO_PACKAGING_TOKEN_RE = re.compile(
    r"\b(?:ВИКТОРИЯ|МАЙКА|ПАКЕТ-МАЙКА|ПАКЕТ|ФАС|ФАС\.|УПАК|УПАК\.|ПРОМО)\b",
    re.IGNORECASE,
)

_UNIT_ALIASES = {
    "Г": "г",
    "ГР": "г",
    "ГР.": "г",
    "КГ": "кг",
    "КГ.": "кг",
    "KT": "кг",
    "KR": "кг",
    "МЛ": "мл",
    "Л": "л",
    "ШТ": "шт",
    "ШТ.": "шт",
    "ПАК": "пак",
    "УП": "упак",
    "УПАК": "упак",
    "УПАК.": "упак",
    "ЯЩ": "ящ",
    "КОРОБ": "короб",
    "БУТ": "бут",
}


def normalize_item_candidate(item: InvoiceParsedItem) -> list[dict[str, str]]:
    """Normalize one model candidate and return deterministic review issues."""
    issues: list[dict[str, str]] = []
    raw_name = _clean_spaces(item.raw_name)
    item.raw_name = raw_name

    extracted_codes = _CODE_RE.findall(raw_name)
    item.codes = _deduplicate([*item.codes, *extracted_codes])

    package, multiplier, accounting_unit = _extract_package(raw_name)
    if package.raw:
        item.package = package
    else:
        item.package = InvoiceItemPackage(
            value=_number(item.package.value),
            unit=_normalize_unit(item.package.unit) or None,
            raw=_clean_spaces(item.package.raw),
        )

    document_unit = _normalize_document_unit(item.document_unit or item.unit)
    item.document_unit = document_unit
    item.unit = document_unit
    quantity = _number(item.quantity_document if item.quantity_document is not None else item.quantity)
    item.quantity_document = quantity
    item.quantity = quantity

    clean_name = _clean_product_name(raw_name, item.package.raw)
    item.clean_name = clean_name or _clean_product_name(item.clean_name, item.package.raw)
    item.normalized_name_candidate = _readable_name(
        _clean_product_name(item.normalized_name_candidate, item.package.raw) or item.clean_name
    )
    item.brand_or_descriptor = _clean_spaces(item.brand_or_descriptor)

    if multiplier is None:
        multiplier, accounting_unit = _fallback_multiplier(item.package, document_unit)
    units_per_package = _positive_number(item.units_per_package)
    item.units_per_package = units_per_package
    multiplier = _apply_units_per_package(multiplier, units_per_package)
    item.quantity_multiplier = multiplier
    item.accounting_unit_candidate = accounting_unit or _normalize_unit(item.accounting_unit_candidate)
    item.accounting_quantity_candidate = _multiply(quantity, multiplier)

    if re.search(r"\b(?:САЛФЕТК|ЛИСТ)", item.clean_name, re.IGNORECASE) and item.package.unit == "л":
        issues.append(_issue("package", "Единица «л» может означать листы, а не литры."))
    if item.needs_review:
        issues.append(_issue("item_normalization", item.review_reason or "Модель отметила строку для проверки."))
    if item.confidence is not None and item.confidence < 0.8:
        issues.append(_issue("confidence", item.review_reason or "Низкая уверенность распознавания товарной строки."))
    if not item.clean_name:
        issues.append(_issue("clean_name", "Не удалось очистить наименование товара."))
    if multiplier is None or not item.accounting_unit_candidate:
        issues.append(_issue("package", "Не удалось уверенно определить фасовку или единицу учета."))

    if issues:
        item.needs_review = True
        item.review_reason = item.review_reason or issues[0]["reason"]
        if item.confidence is None or item.confidence >= 0.8:
            item.confidence = 0.79
    return _deduplicate_issues(issues)


def apply_reference_mapping_to_payload(
    payload: dict[str, Any],
    *,
    products: list[dict[str, Any]],
    packages: list[dict[str, Any]],
    conversion_exceptions: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Map parser candidates to sheet references without model involvement."""
    result = copy.deepcopy(payload)
    metadata = result.setdefault("parser_metadata", {})
    review_flags = metadata.setdefault("review_flags", [])
    corrections = metadata.setdefault("item_corrections", {})
    parser_notes = result.setdefault("parser_notes", [])
    conversion_exceptions = conversion_exceptions or []

    for index, item in enumerate(result.get("items") or [], start=1):
        line_number = int(item.get("line_number") or index)
        item["us_product_name"] = _fallback_us_product_name(item)
        product_match = _match_product(item, products)
        package_match = _match_package(item, packages)
        product_unit = ""

        if product_match["status"] == "matched":
            item["us_product_name"] = product_match["name"]
            item["product_code"] = product_match.get("code")
            item["product_found"] = "Да"
            product_unit = product_match.get("unit") or ""
        elif product_match["status"] == "ambiguous":
            item["product_found"] = "?"
            _add_mapping_problem(
                item,
                line_number,
                "normalized_name_candidate",
                "Товар требует подтверждения сопоставления со справочником «Товары».",
                "Сопоставление",
                review_flags,
                corrections,
                parser_notes,
            )
        else:
            item["product_found"] = "Нет"
            _add_mapping_problem(
                item,
                line_number,
                "normalized_name_candidate",
                "Товар не найден в справочнике «Товары».",
                "Нет в справочнике",
                review_flags,
                corrections,
                parser_notes,
            )

        multiplier, accounting_unit, conversion_method = _calculate_conversion(item)
        package_raw = _package_raw(item)
        if package_match["status"] == "matched":
            item["package_reference_id"] = package_match.get("id")
            package_unit = package_match["accounting_unit"]
            stored_multiplier = package_match["multiplier"]
            if accounting_unit and package_unit and accounting_unit != package_unit:
                multiplier = None
                _add_mapping_problem(
                    item,
                    line_number,
                    "package",
                    "Расчетная единица фасовки не совпадает с единицей в «Справочник фасовок».",
                    "Сопоставление",
                    review_flags,
                    corrections,
                    parser_notes,
                )
            elif stored_multiplier is not None and multiplier is not None and not _numbers_equal(
                stored_multiplier,
                multiplier,
            ):
                item["stored_conversion_factor"] = stored_multiplier
                multiplier = None
                _add_mapping_problem(
                    item,
                    line_number,
                    "conversion_factor",
                    "Расчетный коэффициент не совпадает с сохраненным коэффициентом фасовки.",
                    "Сопоставление",
                    review_flags,
                    corrections,
                    parser_notes,
                )
            elif multiplier is None and stored_multiplier is not None:
                multiplier = stored_multiplier
                accounting_unit = package_unit
                conversion_method = "package_reference"
        elif package_raw and multiplier is None:
            _add_mapping_problem(
                item,
                line_number,
                "package",
                "Фасовка не найдена в листе «Справочник фасовок».",
                "Сопоставление",
                review_flags,
                corrections,
                parser_notes,
            )
        target_unit = product_unit or package_match.get("accounting_unit") or accounting_unit
        if target_unit and accounting_unit and target_unit != accounting_unit:
            exception_match = _match_conversion_exception(
                item,
                product_match,
                conversion_exceptions,
                target_unit=target_unit,
            )
            if exception_match["status"] == "matched":
                multiplier = exception_match["factor"]
                accounting_unit = exception_match["accounting_unit"]
                conversion_method = "product_exception"
                item["conversion_source_id"] = exception_match.get("id")
            else:
                multiplier = None
                reason = (
                    "Найдено несколько подходящих исключений пересчета."
                    if exception_match["status"] == "ambiguous"
                    else "Единица товара не совпадает с расчетной единицей и исключение пересчета не найдено."
                )
                _add_mapping_problem(
                    item,
                    line_number,
                    "accounting_unit_candidate",
                    reason,
                    "Сопоставление",
                    review_flags,
                    corrections,
                    parser_notes,
                )
        elif target_unit:
            accounting_unit = target_unit

        if multiplier is not None and multiplier <= 0:
            multiplier = None
            _add_mapping_problem(
                item,
                line_number,
                "conversion_factor",
                "Коэффициент пересчета должен быть больше нуля.",
                "Сопоставление",
                review_flags,
                corrections,
                parser_notes,
            )

        units_per_package = _positive_number(item.get("units_per_package"))
        item["units_per_package"] = units_per_package
        multiplier = _apply_units_per_package(multiplier, units_per_package)
        conversion_method = _package_method(conversion_method, units_per_package)

        quantity = _number(item.get("quantity_document"))
        if quantity is None:
            quantity = _number(item.get("quantity"))
        item["quantity_multiplier"] = multiplier
        item["conversion_factor"] = multiplier
        item["conversion_method"] = conversion_method if multiplier is not None else "unresolved"
        item["us_unit"] = accounting_unit or ""
        item["quantity_us"] = _multiply(quantity, multiplier)
        price = _number(item.get("price"))
        item["price_us"] = _divide(price, multiplier)
        item["conversion_amount_delta"] = _conversion_amount_delta(
            quantity,
            price,
            item["quantity_us"],
            item["price_us"],
        )
        if (
            item["conversion_amount_delta"] is not None
            and item["conversion_amount_delta"] > settings.conversion_amount_tolerance
        ):
            _add_mapping_problem(
                item,
                line_number,
                "price_us",
                "Пересчитанные количество и цена не сохраняют стоимость строки.",
                "Сопоставление",
                review_flags,
                corrections,
                parser_notes,
            )
        if multiplier is None or not accounting_unit:
            item["conversion_review_reason"] = (
                item.get("review_reason")
                or "Не удалось детерминированно рассчитать количество и цену в УС."
            )
        elif item["price_us"] is None and price is not None:
            item["conversion_review_reason"] = "Цена в УС не рассчитана, хотя коэффициент определен."
            _add_mapping_problem(
                item,
                line_number,
                "price_us",
                item["conversion_review_reason"],
                "Сопоставление",
                review_flags,
                corrections,
                parser_notes,
            )
        item["mapping_status"] = "needs_review" if item.get("correction") else "ready"
        item["mapping_error"] = item.get("review_reason") if item.get("correction") else ""

    if corrections and metadata.get("upload_status") != "Не готово":
        metadata["upload_status"] = "Требует проверки"
        if metadata.get("duplicate") != "Да":
            metadata["row_status"] = "Правка вручную"
    return result


def _calculate_conversion(item: dict[str, Any]) -> tuple[float | None, str, str]:
    raw_name = str(item.get("raw_name") or item.get("name") or "")
    document_unit = _normalize_unit(item.get("document_unit") or item.get("unit"))
    package, multiplier, accounting_unit = _extract_package(raw_name)
    if multiplier is not None:
        if document_unit in {"кг", "л"} and accounting_unit == document_unit and multiplier > 1:
            return 1.0, document_unit, "identity_document_unit"
        method = "compound_package" if _COMPOUND_PACKAGE_RE.search(raw_name) else "standard"
        return multiplier, accounting_unit, method

    package_data = item.get("package") if isinstance(item.get("package"), dict) else {}
    package_value = _number(package_data.get("value"))
    package_unit = _normalize_unit(package_data.get("unit"))
    multiplier, accounting_unit = _convert_package_value(package_value, package_unit)
    if multiplier is not None:
        return multiplier, accounting_unit, "standard"

    if document_unit in {"кг", "л", "шт", "бут"}:
        return 1.0, document_unit, "identity"
    if package.raw:
        return None, "", "unresolved"
    return None, "", "unresolved"

def _positive_number(value: Any) -> float | None:
    number = _number(value)
    if number is None or number <= 0:
        return None
    return number


def _apply_units_per_package(multiplier: float | None, units_per_package: float | None) -> float | None:
    if multiplier is None:
        return None
    if units_per_package is None:
        return multiplier
    return _multiply(multiplier, units_per_package)


def _package_method(method: str, units_per_package: float | None) -> str:
    if units_per_package is None or units_per_package == 1:
        return method
    return f"{method}_with_units_per_package"

def _extract_package(raw_name: str) -> tuple[InvoiceItemPackage, float | None, str]:
    dimension = _DIMENSION_RE.search(raw_name)
    if dimension:
        return (
            InvoiceItemPackage(value=None, unit="см", raw=_clean_spaces(dimension.group(0))),
            None,
            "",
        )

    compound = _COMPOUND_PACKAGE_RE.search(raw_name)
    if compound:
        count = _number(compound.group("count"))
        value = _number(compound.group("value"))
        unit = _normalize_unit(compound.group("unit"))
        base_multiplier, accounting_unit = _convert_package_value(value, unit)
        return (
            InvoiceItemPackage(value=value, unit=unit, raw=_clean_spaces(compound.group(0))),
            _multiply(count, base_multiplier),
            accounting_unit,
        )

    matches = list(_PACKAGE_RE.finditer(raw_name))
    if not matches:
        return InvoiceItemPackage(), None, ""

    primary = matches[0]
    value = _number(primary.group("value"))
    unit = _normalize_unit(primary.group("unit"))
    multiplier, accounting_unit = _convert_package_value(value, unit)
    if len(matches) > 1 and unit in {"г", "кг", "мл", "л"}:
        count_match = next(
            (match for match in matches[1:] if _normalize_unit(match.group("unit")) in {"шт", "пак"}),
            None,
        )
        if count_match:
            multiplier = _multiply(multiplier, _number(count_match.group("value")))
    return (
        InvoiceItemPackage(value=value, unit=unit, raw=_clean_spaces(primary.group(0))),
        multiplier,
        accounting_unit,
    )


def _fallback_multiplier(package: InvoiceItemPackage, document_unit: str) -> tuple[float | None, str]:
    multiplier, accounting_unit = _convert_package_value(_number(package.value), _normalize_unit(package.unit))
    if multiplier is not None:
        return multiplier, accounting_unit
    normalized_document_unit = _normalize_unit(document_unit)
    if normalized_document_unit in {"кг", "л", "шт", "бут"}:
        return 1.0, normalized_document_unit
    return None, ""


def _convert_package_value(value: float | None, unit: str) -> tuple[float | None, str]:
    if value is None:
        return None, ""
    if unit == "г":
        return value / 1000, "кг"
    if unit == "кг":
        return value, "кг"
    if unit == "мл":
        return value / 1000, "л"
    if unit == "л":
        return value, "л"
    if unit in {"шт", "пак", "бут"}:
        return value, "шт" if unit == "шт" else unit
    return None, ""


def _clean_product_name(value: Any, package_raw: str = "") -> str:
    text = _clean_spaces(value)
    if not text:
        return ""
    text = _CODE_RE.sub(" ", text)
    text = re.sub(r"\[[^\]]*\]", " ", text)
    text = re.sub(r"^\s*\d+(?=[A-ZА-ЯЁ])", "", text, flags=re.IGNORECASE)
    text = _TECHNICAL_PREFIX_RE.sub("", text)
    if package_raw:
        text = re.sub(re.escape(package_raw), " ", text, flags=re.IGNORECASE)
    if _PROMO_PACKAGING_TOKEN_RE.search(text):
        original_text = text
        reduced = _PROMO_PACKAGING_TOKEN_RE.sub(" ", text)
        reduced = re.sub(r"\b\d+(?:[.,]\d+)?\s*[*XХ]\s*\d+(?:[.,]\d+)?\s*СМ\b", " ", reduced, flags=re.IGNORECASE)
        reduced = re.sub(r"\s+", " ", reduced).strip()
        if reduced and len(reduced) >= 4:
            text = reduced
        elif re.search(r"\bПАКЕТ\b", original_text, re.IGNORECASE):
            text = "ПАКЕТ"
    text = re.sub(r"^[\[\]():;,.+\-\s]+|[\[\]():;,+\s]+$", "", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def _readable_name(value: str) -> str:
    if not value:
        return ""
    words = []
    for word in value.split():
        words.append(word if re.search(r"\d", word) else word.capitalize())
    return " ".join(words)


def _match_product(item: dict[str, Any], products: list[dict[str, Any]]) -> dict[str, Any]:
    query = (
        item.get("normalized_name_candidate")
        or item.get("clean_name")
        or item.get("name")
        or item.get("raw_name")
        or ""
    )
    ranked = []
    for product in products:
        name = _catalog_value(product, "Наименование", "name")
        if not name:
            continue
        ranked.append((_name_similarity(query, name), name, product))
    ranked.sort(key=lambda candidate: candidate[0], reverse=True)
    if not ranked or ranked[0][0] < 0.62:
        return {"status": "missing"}
    best_score, best_name, best = ranked[0]
    second_score = ranked[1][0] if len(ranked) > 1 else 0
    if best_score < 0.82 or second_score >= best_score - 0.04:
        return {"status": "ambiguous", "name": best_name, "score": best_score}
    return {
        "status": "matched",
        "name": best_name,
        "code": _catalog_value(best, "Код", "code"),
        "unit": _normalize_unit(_catalog_value(best, "Ед. изм.", "unit")),
        "score": best_score,
    }


def _match_package(item: dict[str, Any], packages: list[dict[str, Any]]) -> dict[str, Any]:
    package_raw = _normalize_compact(_package_raw(item))
    if not package_raw:
        return {"status": "not_required"}
    for package in packages:
        if str(_catalog_value(package, "Активна", "active") or "").strip().lower() in {"нет", "false", "0"}:
            continue
        variants = [
            _catalog_value(package, "Фасовка в документе", "document_package"),
            _catalog_value(package, "Основная фасовка", "primary_package"),
        ]
        variants.extend(str(_catalog_value(package, "Варианты", "variants") or "").split(";"))
        if package_raw not in {_normalize_compact(value) for value in variants if value}:
            continue
        return {
            "status": "matched",
            "id": _catalog_value(package, "ID", "id"),
            "multiplier": _number(_catalog_value(package, "Коэффициент пересчета", "multiplier")),
            "accounting_unit": _normalize_unit(
                _catalog_value(package, "Единица учета в УС", "accounting_unit")
            ),
        }
    return {"status": "missing"}


def _match_conversion_exception(
    item: dict[str, Any],
    product_match: dict[str, Any],
    exceptions: list[dict[str, Any]],
    *,
    target_unit: str,
) -> dict[str, Any]:
    document_unit = _normalize_unit(item.get("document_unit") or item.get("unit"))
    item_names = {
        _normalize_name(item.get("normalized_name_candidate")),
        _normalize_name(item.get("clean_name")),
        _normalize_name(product_match.get("name")),
    }
    item_names.discard("")
    raw_name = _normalize_name(item.get("raw_name") or item.get("name"))
    matches = []
    for exception in exceptions:
        if str(_catalog_value(exception, "Активна", "active") or "да").strip().lower() in {
            "нет",
            "false",
            "0",
        }:
            continue
        exception_name = _normalize_name(
            _catalog_value(
                exception,
                "Наименование товара",
                "Товар",
                "product_name",
            )
        )
        if not exception_name or exception_name not in item_names:
            continue
        exception_document_unit = _normalize_unit(
            _catalog_value(
                exception,
                "Ед.изм. в документе",
                "Единица документа",
                "document_unit",
            )
        )
        exception_accounting_unit = _normalize_unit(
            _catalog_value(
                exception,
                "Ед.изм. в УС",
                "Единица учета в УС",
                "accounting_unit",
            )
        )
        if exception_document_unit and exception_document_unit != document_unit:
            continue
        if exception_accounting_unit and exception_accounting_unit != target_unit:
            continue
        qualifier = _normalize_name(
            _catalog_value(exception, "Вариант", "Квалификатор", "qualifier")
        )
        if qualifier and qualifier not in raw_name:
            continue
        factor = _number(
            _catalog_value(
                exception,
                "Коэффициент пересчета",
                "Вес 1 шт",
                "factor",
            )
        )
        if factor is None or factor <= 0:
            continue
        matches.append(
            {
                "status": "matched",
                "id": _catalog_value(exception, "ID", "id"),
                "factor": factor,
                "accounting_unit": exception_accounting_unit or target_unit,
            }
        )
    if not matches:
        return {"status": "missing"}
    unique = {
        (match["factor"], match["accounting_unit"], match.get("id"))
        for match in matches
    }
    if len(unique) > 1:
        return {"status": "ambiguous"}
    return matches[0]


def _add_mapping_problem(
    item: dict[str, Any],
    line_number: int,
    field: str,
    reason: str,
    correction: str,
    review_flags: list[dict[str, Any]],
    corrections: dict[Any, str],
    parser_notes: list[str],
) -> None:
    if not item.get("correction"):
        item["correction"] = correction
    item["needs_review"] = True
    item["review_reason"] = item.get("review_reason") or reason
    flag = {
        "scope": "item",
        "line_number": line_number,
        "field": field,
        "reason": reason,
        "severity": "warning",
    }
    if flag not in review_flags:
        review_flags.append(flag)
    corrections[line_number] = item["correction"]
    if reason not in parser_notes:
        parser_notes.append(reason)


def _name_similarity(left: Any, right: Any) -> float:
    left_normalized = _normalize_name(left)
    right_normalized = _normalize_name(right)
    if not left_normalized or not right_normalized:
        return 0.0
    if left_normalized == right_normalized:
        return 1.0
    if left_normalized in right_normalized or right_normalized in left_normalized:
        shorter = min(len(left_normalized), len(right_normalized))
        longer = max(len(left_normalized), len(right_normalized))
        return max(0.84, shorter / longer)
    left_tokens = set(left_normalized.split())
    right_tokens = set(right_normalized.split())
    token_score = len(left_tokens & right_tokens) / max(len(left_tokens | right_tokens), 1)
    return max(SequenceMatcher(None, left_normalized, right_normalized).ratio(), token_score)


def _normalize_name(value: Any) -> str:
    text = str(value or "").lower().replace("ё", "е")
    text = re.sub(r"[^a-zа-я0-9%]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _normalize_compact(value: Any) -> str:
    return re.sub(r"[\s.,]+", "", str(value or "").lower().replace("x", "х"))


def _normalize_document_unit(value: Any) -> str:
    normalized = _normalize_unit(value)
    return normalized.upper() if normalized else ""


def _normalize_unit(value: Any) -> str:
    text = _clean_spaces(value).upper()
    return _UNIT_ALIASES.get(text, text.lower())


def _package_raw(item: dict[str, Any]) -> str:
    package = item.get("package") or {}
    return _clean_spaces(package.get("raw") if isinstance(package, dict) else "")


def _catalog_value(row: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        if row.get(key) not in (None, ""):
            return row[key]
    return None


def _fallback_us_product_name(item: dict[str, Any]) -> str:
    for key in ("normalized_name_candidate", "clean_name", "name", "raw_name"):
        value = _clean_spaces(item.get(key))
        if value:
            return value
    return ""


def _number(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(Decimal(str(value).replace(" ", "").replace(",", ".")))
    except (InvalidOperation, ValueError):
        return None


def _multiply(left: Any, right: Any) -> float | None:
    left_number = _decimal(left)
    right_number = _decimal(right)
    if left_number is None or right_number is None:
        return None
    return float(round(left_number * right_number, 6))


def _divide(left: Any, right: Any) -> float | None:
    left_number = _decimal(left)
    right_number = _decimal(right)
    if left_number is None or right_number is None or right_number <= 0:
        return None
    return float(round(left_number / right_number, 6))


def _conversion_amount_delta(
    quantity_document: Any,
    price_document: Any,
    quantity_us: Any,
    price_us: Any,
) -> float | None:
    values = [
        _decimal(quantity_document),
        _decimal(price_document),
        _decimal(quantity_us),
        _decimal(price_us),
    ]
    if any(value is None for value in values):
        return None
    quantity_doc, price_doc, quantity_accounting, price_accounting = values
    delta = abs(
        (quantity_doc * price_doc)
        - (quantity_accounting * price_accounting)
    )
    return float(round(delta, 6))


def _numbers_equal(left: Any, right: Any, tolerance: str = "0.000001") -> bool:
    left_number = _decimal(left)
    right_number = _decimal(right)
    if left_number is None or right_number is None:
        return False
    return abs(left_number - right_number) <= Decimal(tolerance)


def _decimal(value: Any) -> Decimal | None:
    if value in (None, ""):
        return None
    try:
        return Decimal(str(value).replace(" ", "").replace(",", "."))
    except (InvalidOperation, ValueError):
        return None


def _clean_spaces(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _deduplicate(values: list[str]) -> list[str]:
    result = []
    for value in values:
        normalized = _clean_spaces(value)
        if normalized and normalized not in result:
            result.append(normalized)
    return result


def _issue(field: str, reason: str) -> dict[str, str]:
    return {"field": field, "reason": reason, "severity": "warning"}


def _deduplicate_issues(issues: list[dict[str, str]]) -> list[dict[str, str]]:
    result = []
    for issue in issues:
        if issue not in result:
            result.append(issue)
    return result
