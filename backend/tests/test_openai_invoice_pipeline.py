from types import SimpleNamespace
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.schemas.invoice_parser import InvoiceParsedItem, InvoiceParserResult
from app.services.item_normalization_service import (
    apply_reference_mapping_to_payload,
    normalize_item_candidate,
)
from app.services.invoice_normalization_service import (
    normalize_invoice_result,
    normalize_supplier_inn_value,
    to_legacy_invoice_payload,
)
from app.services.normalization import canonical_invoice_number
from app.routers.invoice_review import _apply_duplicate_status
from app.services.openai_invoice_parser_service import SYSTEM_PROMPT, parse_invoice_with_openai
from app.services.invoice_review_service import ensure_upload_status_allows_send


def _parsed_invoice(**overrides):
    payload = {
        "document": {
            "document_date": "04.07.2026",
            "document_number": "A-42",
            "supplier_name": 'ООО "Поставщик"',
            "supplier_inn": "39 0004 0690",
            "shipper": "",
            "receiver": 'ООО "Кафе"',
            "basis": "Договор 1",
            "total_without_vat": 100,
            "vat_total": 20,
            "total_with_vat": 120,
        },
        "items": [
            {
                "line_number": 1,
                "raw_name": "Молоко",
                "unit": "шт",
                "quantity": 2,
                "price": 50,
                "amount_without_vat": 100,
                "vat_rate": "20",
                "vat_amount": 20,
                "amount_with_vat": 120,
                "confidence": 0.98,
                "source_fragment": "Товар 2 шт 50 100 НДС 20 120",
            }
        ],
        "review_flags": [],
        "source_trace": {
            "source_type": "image",
            "ocr_used": True,
            "extraction_method": "google_drive_ocr",
            "raw_text_sample": "sample",
        },
    }
    payload.update(overrides)
    return payload


def test_parser_contract_forbids_unknown_fields():
    payload = _parsed_invoice()
    payload["unexpected"] = True

    with pytest.raises(ValidationError):
        InvoiceParserResult.model_validate(payload)


def test_parser_contract_forbids_unknown_package_fields():
    payload = _parsed_invoice()
    payload["items"][0]["package"] = {"value": 1, "unit": "л", "raw": "1Л", "unknown": True}

    with pytest.raises(ValidationError):
        InvoiceParserResult.model_validate(payload)


def test_openai_parser_uses_structured_response_and_returns_legacy_payload(tmp_path, monkeypatch):
    parsed = InvoiceParserResult.model_validate(_parsed_invoice())

    class FakeResponses:
        def __init__(self):
            self.kwargs = None

        def parse(self, **kwargs):
            self.kwargs = kwargs
            return SimpleNamespace(output_parsed=parsed)

    responses = FakeResponses()
    client = SimpleNamespace(responses=responses)
    monkeypatch.setattr("app.services.openai_invoice_parser_service.settings.openai_debug_log_enabled", False)

    result = parse_invoice_with_openai(
        {
            "filename": "invoice.jpg",
            "source_type": "image",
            "ocr_used": True,
            "extraction_method": "google_drive_ocr",
            "raw_text": "invoice evidence",
        },
        client=client,
    )

    assert responses.kwargs["text_format"] is InvoiceParserResult
    assert responses.kwargs["input"][0]["content"][0]["type"] == "input_text"
    assert result["parser_provider"] == "openai"
    assert result["invoice_date"] == "2026-07-04"
    assert result["supplier_inn"] == "3900040690"
    assert result["items"][0]["sum"] == 100.0
    assert result["parser_metadata"]["upload_status"] == "Проверить"
    assert "Справочник фасовок" in SYSTEM_PROMPT
    assert "quantity_multiplier" in SYSTEM_PROMPT


def test_openai_parser_sends_prepared_pages_as_ordered_image_inputs(tmp_path: Path, monkeypatch):
    parsed = InvoiceParserResult.model_validate(_parsed_invoice())
    first = tmp_path / "page-1.jpg"
    second = tmp_path / "page-2.jpg"
    first.write_bytes(b"first")
    second.write_bytes(b"second")

    class FakeResponses:
        def __init__(self):
            self.kwargs = None

        def parse(self, **kwargs):
            self.kwargs = kwargs
            return SimpleNamespace(output_parsed=parsed)

    responses = FakeResponses()
    monkeypatch.setattr("app.services.openai_invoice_parser_service.settings.openai_debug_log_enabled", False)
    parse_invoice_with_openai(
        {
            "filename": "invoice",
            "source_type": "image",
            "raw_text": "OCR evidence",
            "page_sources": [
                {
                    "page_number": 1,
                    "filename": "page-1.jpg",
                    "source_type": "image",
                    "original_path": str(first),
                },
                {
                    "page_number": 2,
                    "filename": "page-2.jpg",
                    "source_type": "image",
                    "original_path": str(second),
                },
            ],
        },
        client=SimpleNamespace(responses=responses),
    )

    content = responses.kwargs["input"][0]["content"]
    assert [entry["type"] for entry in content] == [
        "input_text",
        "input_text",
        "input_image",
        "input_text",
        "input_image",
    ]
    assert "Страница 1" in content[1]["text"]
    assert content[2]["image_url"].startswith("data:image/jpeg;base64,")


def test_item_normalization_extracts_code_package_and_accounting_quantity():
    item = InvoiceParsedItem(
        raw_name="3 ТОВАР : ШТ. [N+13968 КЕФИР ФЕРМЕРСКИЙ 800Г",
        unit="ШТ",
        quantity=5,
        price=100,
        confidence=0.95,
    )

    issues = normalize_item_candidate(item)

    assert issues == []
    assert item.clean_name == "КЕФИР ФЕРМЕРСКИЙ"
    assert item.normalized_name_candidate == "Кефир Фермерский"
    assert item.codes == ["N+13968"]
    assert item.package.model_dump() == {"value": 800.0, "unit": "г", "raw": "800Г"}
    assert item.quantity_multiplier == 0.8
    assert item.accounting_quantity_candidate == 4.0
    assert item.accounting_unit_candidate == "кг"



def test_item_normalization_uses_explicit_units_per_package_from_document_column():
    item = InvoiceParsedItem(
        raw_name="1,5Л ВОДА КРАСНЫЙ КЛЮЧ ГАЗ",
        document_unit="УП",
        quantity_document=2,
        units_per_package=6,
        confidence=0.95,
    )

    issues = normalize_item_candidate(item)

    assert issues == []
    assert item.package.model_dump() == {"value": 1.5, "unit": "л", "raw": "1,5Л"}
    assert item.units_per_package == 6
    assert item.quantity_multiplier == 9.0
    assert item.accounting_quantity_candidate == 18.0
    assert item.accounting_unit_candidate == "л"


def test_reference_mapping_applies_units_per_package_after_package_reference_check():
    payload = {
        "items": [
            {
                "line_number": 4,
                "name": "1,5Л ВОДА КРАСНЫЙ КЛЮЧ ГАЗ",
                "raw_name": "1,5Л ВОДА КРАСНЫЙ КЛЮЧ ГАЗ",
                "normalized_name_candidate": "Вода Красный Ключ Газ",
                "package": {"value": 1.5, "unit": "л", "raw": "1,5Л"},
                "document_unit": "УП",
                "quantity": 2,
                "quantity_document": 2,
                "units_per_package": 6,
                "price": 181.48,
            }
        ],
        "parser_metadata": {
            "upload_status": "Проверить",
            "row_status": "Распознано",
            "review_flags": [],
            "item_corrections": {},
        },
        "parser_notes": [],
    }

    result = apply_reference_mapping_to_payload(
        payload,
        products=[{"Наименование": "Вода Красный Ключ Газ", "Код": "277029", "Ед. изм.": "л"}],
        packages=[
            {
                "ID": "1-50000",
                "Фасовка в документе": "1,5Л",
                "Коэффициент пересчета": 1.5,
                "Единица учета в УС": "л",
                "Активна": "да",
            }
        ],
    )

    item = result["items"][0]
    assert item["units_per_package"] == 6
    assert item["conversion_factor"] == 9.0
    assert item["quantity_us"] == 18.0
    assert item["price_us"] == pytest.approx(181.48 / 9)
    assert item["conversion_method"] == "standard_with_units_per_package"
    assert item.get("correction", "") == ""

def test_item_normalization_fixes_common_latin_kg_ocr_alias():
    item = InvoiceParsedItem(
        raw_name="1Еноки вес",
        document_unit="KT",
        quantity_document=3.14,
        price=650,
        confidence=0.9,
    )

    issues = normalize_item_candidate(item)

    assert issues == []
    assert item.clean_name == "Еноки вес"
    assert item.document_unit == "КГ"
    assert item.accounting_unit_candidate == "кг"
    assert item.quantity_multiplier == 1.0


def test_item_normalization_multiplies_nested_liquid_package():
    item = InvoiceParsedItem(
        raw_name="ВОДА ПИТЬЕВАЯ 0,5Л 12ШТ",
        document_unit="УПАК",
        quantity_document=3,
        confidence=0.9,
    )

    issues = normalize_item_candidate(item)

    assert issues == []
    assert item.package.model_dump() == {"value": 0.5, "unit": "л", "raw": "0,5Л"}
    assert item.quantity_multiplier == 6.0
    assert item.accounting_quantity_candidate == 18.0
    assert item.accounting_unit_candidate == "л"


def test_item_normalization_keeps_identity_for_weight_document_unit_even_if_raw_name_contains_weight():
    item = InvoiceParsedItem(
        raw_name='Окорок "Пармский" 3,954 КГ',
        document_unit="КГ",
        quantity_document=3.954,
        quantity=3.954,
        confidence=0.95,
    )

    issues = normalize_item_candidate(item)

    assert issues == []
    payload = {
        "items": [
            {
                "line_number": 1,
                "name": item.raw_name,
                "raw_name": item.raw_name,
                "document_unit": item.document_unit,
                "quantity_document": item.quantity_document,
                "quantity": item.quantity,
                "price": 2250,
            }
        ],
        "parser_metadata": {
            "upload_status": "Проверить",
            "row_status": "Распознано",
            "review_flags": [],
            "item_corrections": {},
        },
        "parser_notes": [],
    }

    result = apply_reference_mapping_to_payload(payload, products=[], packages=[])
    mapped = result["items"][0]
    assert mapped["conversion_method"] in {"identity", "identity_document_unit"}
    assert mapped["quantity_us"] == 3.954
    assert mapped["price_us"] == 2250.0


def test_item_normalization_cleans_packaging_noise_for_fallback_name():
    item = InvoiceParsedItem(
        raw_name="7 ТОВАР : ШТ. ПАКЕТ-МАЙКА ВИКТОРИЯ 65*40СМ",
        unit="ШТ",
        quantity=1,
        price=9.9,
        confidence=0.95,
    )

    normalize_item_candidate(item)

    assert item.clean_name == "ПАКЕТ"
    assert item.normalized_name_candidate == "Пакет"


def test_item_normalization_flags_ambiguous_sheets_unit():
    item = InvoiceParsedItem(
        raw_name="САЛФЕТКИ БУМ 24Х24 100Л",
        document_unit="УПАК",
        quantity_document=1,
        confidence=0.9,
    )

    issues = normalize_item_candidate(item)

    assert item.needs_review is True
    assert any("листы" in issue["reason"] for issue in issues)


def test_reference_mapping_fills_us_fields_deterministically():
    payload = {
        "items": [
            {
                "line_number": 1,
                "name": "КЕФИР ФЕРМЕРСКИЙ 800Г",
                "normalized_name_candidate": "Кефир Фермерский",
                "package": {"value": 800, "unit": "г", "raw": "800Г"},
                "quantity": 5,
                "quantity_document": 5,
                "price": 100,
                "quantity_multiplier": 0.8,
                "accounting_unit_candidate": "кг",
            }
        ],
        "parser_metadata": {
            "upload_status": "Проверить",
            "row_status": "Распознано",
            "review_flags": [],
            "item_corrections": {},
        },
        "parser_notes": [],
    }

    result = apply_reference_mapping_to_payload(
        payload,
        products=[{"Наименование": "Кефир", "Код": "01-00017", "Ед. изм.": "кг"}],
        packages=[
            {
                "ID": "0-00800",
                "Фасовка в документе": "800 г",
                "Коэффициент пересчета": 0.8,
                "Единица учета в УС": "кг",
                "Активна": "да",
            }
        ],
    )

    item = result["items"][0]
    assert item["product_found"] == "Да"
    assert item["us_product_name"] == "Кефир"
    assert item["us_unit"] == "кг"
    assert item["quantity_us"] == 4.0
    assert item["price_us"] == 125.0
    assert item["conversion_factor"] == 0.8
    assert item["conversion_method"] == "standard"
    assert item["conversion_amount_delta"] == 0.0
    assert item.get("correction", "") == ""


def test_reference_mapping_marks_missing_product_and_package():
    payload = {
        "items": [
            {
                "line_number": 2,
                "name": "НЕИЗВЕСТНЫЙ ПРОДУКТ 333Г",
                "normalized_name_candidate": "Неизвестный продукт",
                "package": {"value": 333, "unit": "г", "raw": "333Г"},
                "quantity": 2,
                "quantity_document": 2,
                "quantity_multiplier": 0.333,
                "accounting_unit_candidate": "кг",
                "correction": "",
            }
        ],
        "parser_metadata": {
            "upload_status": "Проверить",
            "row_status": "Распознано",
            "review_flags": [],
            "item_corrections": {},
        },
        "parser_notes": [],
    }

    result = apply_reference_mapping_to_payload(
        payload,
        products=[{"Наименование": "Кефир", "Код": "01-00017", "Ед. изм.": "л"}],
        packages=[{"Фасовка в документе": "250г", "Коэффициент пересчета": 0.25}],
    )

    item = result["items"][0]
    assert item["product_found"] == "Нет"
    assert item["us_product_name"] == "Неизвестный продукт"
    assert item["correction"] == "Нет в справочнике"
    assert result["parser_metadata"]["upload_status"] == "Требует проверки"
    assert result["parser_metadata"]["row_status"] == "Правка вручную"
    assert len(result["parser_metadata"]["review_flags"]) == 1
    assert item["quantity_us"] == 0.666


def test_normalization_clears_basis_when_it_repeats_document_form():
    result = normalize_invoice_result(
        _parsed_invoice(
            document={
                "document_date": "23.06.2026",
                "document_number": "1928",
                "document_form": "УПД",
                "supplier_name": 'ООО "ФРУКТЫ АРИФА"',
                "supplier_inn": "3900040690",
                "shipper": 'ООО "ЛИР"',
                "receiver": 'ООО "ЛИР"',
                "basis": "Универсальный передаточный документ No1928 от 23 июня 2026 г",
                "total_without_vat": 2041,
                "vat_total": 0,
                "total_with_vat": 2041,
            }
        )
    )

    assert result.document.basis == ""
    assert any(flag.field == "basis" for flag in result.review_flags)


def test_receipt_defaults_fill_vat_fields_when_missing():
    result = normalize_invoice_result(
        _parsed_invoice(
            document={
                "document_date": "23.06.2026 18:04",
                "document_number": "ЧЕК 0245",
                "document_form": "Чек",
                "supplier_name": 'ООО "ВИКТОРИЯ БАЛТИЯ"',
                "supplier_inn": "3905069220",
                "shipper": "",
                "receiver": "",
                "basis": "",
                "total_without_vat": 72.9,
                "vat_total": None,
                "total_with_vat": 72.9,
            },
            items=[
                {
                    "line_number": 1,
                    "raw_name": "КЕФИР ФЕРМЕРСКИЙ 800Г",
                    "unit": "шт",
                    "quantity": 1,
                    "price": 72.9,
                    "amount_without_vat": 72.9,
                    "vat_rate": "",
                    "vat_amount": None,
                    "amount_with_vat": None,
                    "confidence": 0.95,
                    "source_fragment": "ЧЕК",
                }
            ],
        )
    )

    item = result.items[0]
    assert item.vat_rate == "Без НДС"
    assert item.vat_amount == 0.0
    assert item.amount_with_vat == 72.9


def test_canonical_invoice_number_normalizes_latin_prefix_and_receipt_prefix():
    assert canonical_invoice_number("UPMK3003248", document_form="ТОРГ-12") == "УПМК3003248"
    assert canonical_invoice_number("УПМК3003248", document_form="ТОРГ-12") == "УПМК3003248"
    assert canonical_invoice_number("ЧЕК 0245", document_form="Чек") == "0245"


def test_duplicate_status_uses_canonical_invoice_number():
    class _Doc:
        def __init__(self, invoice_number, supplier_legal_name, invoice_date, recognized_items_json="{}"):
            self.invoice_number = invoice_number
            self.supplier_legal_name = supplier_legal_name
            self.invoice_date = invoice_date
            self.recognized_items_json = recognized_items_json

    class _Query:
        def __init__(self, docs):
            self.docs = docs

        def all(self):
            return self.docs

    class _DB:
        def __init__(self, docs):
            self.docs = docs

        def query(self, _model):
            return _Query(self.docs)

    parsed = {
        "invoice_number": "UPMK3003248",
        "document_form": "ТОРГ-12",
        "supplier": 'ООО "МК "Залесье"',
        "invoice_date": "2026-06-22",
        "total_sum": 13303.32,
        "parser_metadata": {},
    }
    db = _DB([_Doc("УПМК3003248", 'ООО "МК "Залесье"', "2026-06-22")])

    _apply_duplicate_status(db, parsed)

    assert parsed["parser_metadata"]["duplicate"] == "Да"
    assert parsed["parser_metadata"]["upload_status"] == "Не готово"


def test_reference_mapping_uses_piece_weight_exception():
    payload = {
        "items": [
            {
                "line_number": 1,
                "name": "ЯЙЦО КУРИНОЕ С1",
                "normalized_name_candidate": "Яйцо С1",
                "document_unit": "шт",
                "unit": "шт",
                "quantity": 30,
                "quantity_document": 30,
                "price": 10,
                "package": {},
            }
        ],
        "parser_metadata": {
            "upload_status": "Проверить",
            "row_status": "Распознано",
            "review_flags": [],
            "item_corrections": {},
        },
        "parser_notes": [],
    }

    result = apply_reference_mapping_to_payload(
        payload,
        products=[{"Наименование": "Яйцо С1", "Код": "EGG-C1", "Ед. изм.": "кг"}],
        packages=[],
        conversion_exceptions=[
            {
                "ID": "egg-c1",
                "Наименование товара": "Яйцо С1",
                "Ед.изм. в документе": "шт",
                "Ед.изм. в УС": "кг",
                "Вес 1 шт": "0,055",
                "Активна": "да",
            }
        ],
    )

    item = result["items"][0]
    assert item["conversion_method"] == "product_exception"
    assert item["conversion_factor"] == 0.055
    assert item["quantity_us"] == 1.65
    assert item["price_us"] == pytest.approx(181.818182)
    assert item["conversion_source_id"] == "egg-c1"


def test_reference_mapping_rejects_ambiguous_product_exception():
    payload = {
        "items": [
            {
                "line_number": 1,
                "name": "АВОКАДО",
                "normalized_name_candidate": "Авокадо",
                "document_unit": "шт",
                "unit": "шт",
                "quantity": 2,
                "quantity_document": 2,
                "price": 100,
                "package": {},
            }
        ],
        "parser_metadata": {
            "upload_status": "Проверить",
            "row_status": "Распознано",
            "review_flags": [],
            "item_corrections": {},
        },
        "parser_notes": [],
    }
    exceptions = [
        {
            "ID": "avocado-350",
            "Товар": "Авокадо",
            "Единица документа": "шт",
            "Единица учета в УС": "кг",
            "Коэффициент пересчета": 0.35,
        },
        {
            "ID": "avocado-400",
            "Товар": "Авокадо",
            "Единица документа": "шт",
            "Единица учета в УС": "кг",
            "Коэффициент пересчета": 0.4,
        },
    ]

    result = apply_reference_mapping_to_payload(
        payload,
        products=[{"Наименование": "Авокадо", "Ед. изм.": "кг"}],
        packages=[],
        conversion_exceptions=exceptions,
    )

    item = result["items"][0]
    assert item["conversion_method"] == "unresolved"
    assert item["quantity_us"] is None
    assert item["price_us"] is None
    assert item["correction"] == "Сопоставление"
    assert "несколько" in item["review_reason"].lower()


def test_reference_mapping_rejects_conflicting_stored_factor():
    payload = {
        "items": [
            {
                "line_number": 1,
                "name": "ПЕРЕЦ 500Г",
                "normalized_name_candidate": "Перец",
                "document_unit": "шт",
                "quantity": 2,
                "price": 200,
                "package": {"value": 500, "unit": "г", "raw": "500Г"},
            }
        ],
        "parser_metadata": {
            "upload_status": "Проверить",
            "row_status": "Распознано",
            "review_flags": [],
            "item_corrections": {},
        },
        "parser_notes": [],
    }

    result = apply_reference_mapping_to_payload(
        payload,
        products=[{"Наименование": "Перец", "Ед. изм.": "кг"}],
        packages=[
            {
                "ID": "pepper-500",
                "Фасовка в документе": "500Г",
                "Единица учета в УС": "кг",
                "Коэффициент пересчета": 0.4,
            }
        ],
    )

    item = result["items"][0]
    assert item["conversion_method"] == "unresolved"
    assert item["stored_conversion_factor"] == 0.4
    assert item["correction"] == "Сопоставление"


@pytest.mark.parametrize(
    ("raw_value", "expected"),
    [
        ("3900040690/390001001", "3900040690"),
        ("3906406206390601001", "3906406206"),
        ("3900040690390001001", "3900040690"),
        ("77 0123 4567", "7701234567"),
    ],
)
def test_supplier_inn_normalization_extracts_real_inn(raw_value, expected):
    assert normalize_supplier_inn_value(raw_value) == expected


def test_invalid_inn_checksum_requires_review():
    normalized = normalize_invoice_result(
        InvoiceParserResult.model_validate(
            _parsed_invoice(
                document={
                    **_parsed_invoice()["document"],
                    "supplier_inn": "7701234567",
                }
            )
        )
    )

    assert normalized.upload_status == "Требует проверки"
    assert any(
        flag.field == "supplier_inn" and "Контрольная сумма" in flag.reason
        for flag in normalized.review_flags
    )


@pytest.mark.parametrize(
    ("raw_date", "expected"),
    [
        ("23 июня 2026 г.", "2026-06-23"),
        ("23 июня 2026 года", "2026-06-23"),
        ("23.06.26 18:03", "2026-06-23"),
    ],
)
def test_russian_document_date_is_normalized(raw_date, expected):
    payload = _parsed_invoice()
    payload["document"]["document_date"] = raw_date

    normalized = normalize_invoice_result(InvoiceParserResult.model_validate(payload))

    assert normalized.document.document_date == expected
    assert not any(flag.field == "document_date" for flag in normalized.review_flags)


@pytest.mark.parametrize(
    ("case", "mutator", "duplicate", "expected_upload", "expected_row", "correction"),
    [
        ("normal", lambda data: None, "", "Проверить", "Распознано", ""),
        (
            "poor_line",
            lambda data: data["items"][0].update(raw_name="", confidence=0.2),
            "",
            "Требует проверки",
            "Правка вручную",
            "Другое",
        ),
        (
            "not_in_catalog",
            lambda data: data["review_flags"].append(
                {
                    "scope": "item",
                    "line_number": 1,
                    "field": "raw_name",
                    "reason": "Товар отсутствует в справочнике.",
                    "severity": "warning",
                }
            ),
            "",
            "Требует проверки",
            "Правка вручную",
            "Нет в справочнике",
        ),
        (
            "total_mismatch",
            lambda data: data["document"].update(total_with_vat=999),
            "",
            "Требует проверки",
            "Правка вручную",
            "",
        ),
        ("possible_duplicate", lambda data: None, "?", "Требует проверки", "Распознано", ""),
        ("confirmed_duplicate", lambda data: None, "Да", "Не готово", "Распознано", ""),
    ],
)
def test_golden_invoice_scenarios(
    case,
    mutator,
    duplicate,
    expected_upload,
    expected_row,
    correction,
):
    data = _parsed_invoice()
    mutator(data)

    result = normalize_invoice_result(data, duplicate=duplicate)
    payload = to_legacy_invoice_payload(result)

    assert result.upload_status == expected_upload, case
    assert result.row_status == expected_row, case
    assert payload["parser_metadata"]["duplicate"] == duplicate
    if correction:
        assert payload["items"][0]["correction"] == correction


def test_ocr_error_has_deterministic_not_ready_status():
    result = normalize_invoice_result(_parsed_invoice(), ocr_error="provider unavailable")

    assert result.upload_status == "Не готово"
    assert result.row_status == "Ошибка загрузки"
    assert result.item_corrections == {1: "Ошибка OCR"}


@pytest.mark.parametrize("status", ["Не готово", "Требует проверки", "Проверить"])
def test_only_upload_status_can_be_sent_to_accounting(status):
    with pytest.raises(ValueError, match="требуется 'Загрузить'"):
        ensure_upload_status_allows_send(status)

    ensure_upload_status_allows_send("Загрузить")
