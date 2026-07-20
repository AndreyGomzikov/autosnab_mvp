import os
import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ["DATABASE_URL"] = "sqlite:///:memory:"

from app.config import settings  # noqa: E402
from app.services.google_sheets_service import (  # noqa: E402
    SHARED_INVOICE_HEADERS,
    _align_shared_rows_to_target_headers,
    _insert_into_existing_spreadsheet,
    _remap_source_rows_to_shared_sheet,
    _table_rows_as_dicts,
    load_invoice_reference_catalogs,
)
from app.services.invoice_review_service import (  # noqa: E402
    INVOICE_REGISTER_HEADERS,
    _invoice_register_item_row,
)


class _FakeValuesResource:
    def __init__(self):
        self.updated = []

    def update(self, **kwargs):
        self.updated.append(kwargs)
        return self

    def execute(self):
        return {}


class _FakeSpreadsheetsResource:
    def __init__(self):
        self.values_resource = _FakeValuesResource()
        self.batch_updates = []
        self.get_calls = []

    def get(self, **kwargs):
        self.get_calls.append(kwargs)
        return _FakeExecute(
            {
                "spreadsheetUrl": "https://docs.google.com/spreadsheets/d/test-id/edit",
                "sheets": [
                    {
                        "properties": {
                            "sheetId": 321,
                            "title": "Накладная",
                            "gridProperties": {"rowCount": 100, "columnCount": 47},
                        }
                    }
                ],
            }
        )

    def batchUpdate(self, **kwargs):
        self.batch_updates.append(kwargs)
        return _FakeExecute({})

    def values(self):
        return self.values_resource


class _FakeSheetsService:
    def __init__(self):
        self.spreadsheets_resource = _FakeSpreadsheetsResource()

    def spreadsheets(self):
        return self.spreadsheets_resource


class _FakeExecute:
    def __init__(self, payload):
        self.payload = payload

    def execute(self):
        return self.payload


class _FakeDocument:
    def __init__(self, file_url, recognized_items_json=None):
        self.file_url = file_url
        self.recognized_items_json = recognized_items_json


class _FakeReceiving:
    def __init__(self, file_url, recognized_items_json=None):
        self.documents = [_FakeDocument(file_url, recognized_items_json=recognized_items_json)]


def test_shared_mapper_uses_headers_and_writes_document_fields_only_on_first_row():
    source = [
        [
            "Статус загрузки", "Статус строки", "Причина ручной корректировки",
            "Индикатор дубля документа", "Дата документа", "№ Документа",
            "Поставщик", "Наименование товара из документа",
        ],
        ["Требует проверки", "Правка вручную", "Другое", "?", "2026-07-04", "42", "Supplier", "Item 1"],
        ["", "", "Нет в справочнике", "", "", "", "", "Item 2"],
    ]

    rows = _remap_source_rows_to_shared_sheet(
        source,
        _FakeReceiving("uploads/invoice.jpg"),
        SHARED_INVOICE_HEADERS,
    )
    indexes = {header: index for index, header in enumerate(SHARED_INVOICE_HEADERS)}

    assert rows[0][indexes["Статус загрузки"]] == "Требует проверки"
    assert rows[0][indexes["Статус строки"]] == "Правка вручную"
    assert rows[1][indexes["Статус загрузки"]] == ""
    assert rows[1][indexes["Статус строки"]] == ""
    assert rows[0][indexes["Корректировка"]] == "Другое"
    assert rows[1][indexes["Корректировка"]] == "Нет в справочнике"
    assert rows[1][indexes["Дата документа"]] == ""


def test_shared_mapper_uses_backend_document_meta_for_shipper_basis_and_correction():
    source = [
        [
            "Статус загрузки", "Статус строки", "Причина ручной корректировки",
            "Индикатор дубля документа", "Форма документа", "ИНН Поставщика",
            "Грузополучатель", "Получатель", "Основание", "Товар найден в справочнике",
            "Наименование товара из документа",
        ],
        [
            "Требует проверки", "Правка вручную", "Другое", "", "УПД", "1234567890",
            "ООО ЛИР", "ООО ЛИР", "Универсальный передаточный документ", "Нет", "Тушка ЦБ",
        ],
    ]
    recognized_items_json = (
        '{"header":{"document_form":"УПД","supplier_inn":"3900040690","shipper":"ООО МК Залесье",'
        '"recipient":"ООО ЛИР","basis":"Основной договор"}}'
    )

    rows = _remap_source_rows_to_shared_sheet(
        source,
        _FakeReceiving("uploads/invoice.jpg", recognized_items_json=recognized_items_json),
        SHARED_INVOICE_HEADERS,
    )
    values = dict(zip(SHARED_INVOICE_HEADERS, rows[0]))

    assert values["Грузоотправитель"] == "ООО МК Залесье"
    assert values["Получатель"] == "ООО ЛИР"
    assert values["Основание"] == "Основной договор"
    assert values["ИНН Поставщика"] == "3900040690"
    assert values["Корректировка"] == "Нет в справочнике"


def test_review_row_uses_only_deterministic_us_mapping_fields():
    header_values = {
        "upload_status": "Проверить",
        "upload_time": "2026-07-04 12:00:00",
        "document_id": 1,
        "duplicate_indicator": "",
        "document_form": "УПД",
        "document_date": "2026-07-04",
        "document_number": "42",
        "supplier": "Supplier",
        "supplier_inn": "1234567890",
        "consignee": "Receiver",
        "recipient": "Receiver",
        "trade_point": "Point",
        "warehouse": "Warehouse",
        "basis": "",
        "total_sum": 800,
        "row_status": "Распознано",
    }
    item = SimpleNamespace(
        received_quantity=5,
        invoice_price=100,
        item_name_from_invoice="КЕФИР ФЕРМЕРСКИЙ 800Г",
        item_name_from_order=None,
        unit="ШТ",
        comment=None,
    )
    row = _invoice_register_item_row(
        header_values,
        item,
        {
            "us_product_name": "Кефир",
            "product_found": "Да",
            "us_unit": "кг",
            "quantity_us": 4,
            "price_us": 125,
            "correction": "",
        },
        1,
    )
    values = dict(zip(INVOICE_REGISTER_HEADERS, row))

    assert values["Наименование товара в УС"] == "Кефир"
    assert values["Товар найден в справочнике"] == "Да"
    assert values["Ед.изм. в УС"] == "кг"
    assert values["Кол-во в УС"] == 4
    assert values["Цена в УС"] == 125


def test_reference_sheet_rows_are_mapped_by_fixed_headers():
    rows = [
        ["Наименование", "Код", "Ед. изм."],
        ["Кефир", "01-00017", "л"],
        ["", "", ""],
    ]

    assert _table_rows_as_dicts(rows) == [
        {"Наименование": "Кефир", "Код": "01-00017", "Ед. изм.": "л"}
    ]


def test_align_shared_rows_to_target_headers_keeps_canonical_width():
    rows = [["x"] * len(SHARED_INVOICE_HEADERS)]
    assert _align_shared_rows_to_target_headers(rows, SHARED_INVOICE_HEADERS) == rows


def test_reference_catalog_loader_reads_fixed_google_sheet_tabs(monkeypatch):
    class ReferenceValues:
        def __init__(self):
            self.kwargs = None

        def batchGet(self, **kwargs):
            self.kwargs = kwargs
            return _FakeExecute(
                {
                    "valueRanges": [
                        {"values": [["Наименование", "Код"], ["Кефир", "01-00017"]]},
                        {"values": [["Поставщик", "Код"], ["ООО Молоко", "SUP-1"]]},
                        {
                            "values": [
                                ["ID", "Фасовка в документе", "Коэффициент пересчета"],
                                ["0-00800", "800 г", 0.8],
                            ]
                        },
                    ]
                }
            )

    class ReferenceSpreadsheets:
        def __init__(self, values):
            self._values = values

        def values(self):
            return self._values

    class ReferenceService:
        def __init__(self, values):
            self._spreadsheets = ReferenceSpreadsheets(values)

        def spreadsheets(self):
            return self._spreadsheets

    values = ReferenceValues()
    old_enabled = settings.google_sheets_enabled
    old_spreadsheet_id = settings.google_target_spreadsheet_id
    settings.google_sheets_enabled = True
    settings.google_target_spreadsheet_id = "sheet-id"
    monkeypatch.setattr(
        "app.services.google_sheets_service._build_google_services",
        lambda: (ReferenceService(values), None),
    )
    try:
        catalogs = load_invoice_reference_catalogs()
    finally:
        settings.google_sheets_enabled = old_enabled
        settings.google_target_spreadsheet_id = old_spreadsheet_id

    assert values.kwargs["ranges"] == [
        "'Товары'!A1:H",
        "'Поставщики'!A1:H",
        "'Справочник фасовок'!A1:M",
    ]
    assert catalogs["products"][0]["Наименование"] == "Кефир"
    assert catalogs["suppliers"][0]["Поставщик"] == "ООО Молоко"
    assert catalogs["packages"][0]["Коэффициент пересчета"] == 0.8


def test_insert_into_existing_spreadsheet_prepends_block_and_separator():
    fake_service = _FakeSheetsService()
    old_sheet_name = settings.google_target_sheet_name
    old_header_count = settings.google_target_header_row_count
    settings.google_target_sheet_name = "Накладная"
    settings.google_target_header_row_count = 2
    try:
        result = _insert_into_existing_spreadsheet(
            receiving=_FakeReceiving("uploads/invoices/test.jpg"),
            sheets_service=fake_service,
            drive_service=None,
            spreadsheet_id="test-id",
            sheet_data={
                "spreadsheet_name": "АвтоСнаб Накладные",
                "primary_sheet_name": "Накладные",
                "sheets": {
                    "Накладные": [
                        [
                            "Статус загрузки",
                            "Статус строки",
                            "Причина ручной корректировки",
                            "Индикатор дубля документа",
                            "Форма документа",
                            "Загрузить в УС",
                            "Дата документа",
                            "№ Документа",
                            "Поставщик",
                            "ИНН Поставщика",
                            "Получатель",
                            "Торговая точка",
                            "Склад",
                            "Основание",
                            "Товар найден в справочнике",
                            "Наименование товара из документа",
                            "Наименование товара в УС",
                            "Ед.изм.",
                            "Ед.изм. в УС",
                            "Кол-во из документа",
                            "Кол-во в УС",
                            "Цена за единицу",
                            "Цена в УС",
                            "Стоимость без НДС",
                            "Ставка НДС %",
                            "Сумма НДС",
                            "Общая стоимость",
                            "Сумма накладной",
                            "Дата приема",
                            "Принял, Ф.И.О.",
                            "Госсистемы",
                            "Кол-во в заявке",
                            "Цена по прайсу",
                            "Последняя дата поставки",
                            "Последняя цена",
                            "Отклонение от цены прайса",
                            "Время загрузки документа",
                            "ID документа",
                        ],
                        [
                            "loaded",
                            "reviewed",
                            "",
                            "no",
                            "paper",
                            "1",
                            "2026-07-03",
                            "doc-1",
                            "Supplier",
                            "1234567890",
                            "Recipient",
                            "Main point",
                            "Warehouse",
                            "Basis",
                            "yes",
                            "item-1",
                            "item-us-1",
                            "kg",
                            "pcs",
                            "10",
                            "9",
                            "100",
                            "95",
                            "1000",
                            "20",
                            "200",
                            "1200",
                            "1200",
                            "2026-07-03",
                            "Operator",
                            "GS",
                            "11",
                            "105",
                            "2026-07-04",
                            "110",
                            "-5",
                            "2026-07-03T10:00:00",
                            "doc-id-1",
                        ],
                        [
                            "loaded",
                            "reviewed",
                            "",
                            "no",
                            "paper",
                            "1",
                            "2026-07-03",
                            "doc-1",
                            "Supplier",
                            "1234567890",
                            "Recipient",
                            "Main point",
                            "Warehouse",
                            "Basis",
                            "yes",
                            "item-2",
                            "item-us-2",
                            "kg",
                            "pcs",
                            "20",
                            "18",
                            "200",
                            "190",
                            "2000",
                            "20",
                            "400",
                            "2400",
                            "2400",
                            "2026-07-03",
                            "Operator",
                            "GS",
                            "22",
                            "205",
                            "2026-07-05",
                            "210",
                            "-10",
                            "2026-07-03T10:00:00",
                            "doc-id-1",
                        ],
                    ]
                },
                "shared_sheet_rows": [
                    [
                        "Проверить", "Распознано", "", "", "УПД", "",
                        "2026-07-03", "doc-1", "Supplier", "1234567890",
                        "Shipper", "Recipient", "Main point", "Warehouse", "Basis",
                        "Да", "item-1", "item-us-1", "kg", "pcs", "10", "", "9",
                        "100", "95", "1000", "20", "200", "1200", "2400", "", "",
                        "GS", "11", "105", "2026-07-04", "110", "-5",
                        "2026-07-03T10:00:00", "doc-id-1", "uploads/invoices/test.jpg",
                    ],
                    [
                        "", "", "Нет в справочнике", "", "", "",
                        "", "", "", "",
                        "", "", "", "", "",
                        "Нет", "item-2", "item-us-2", "kg", "pcs", "20", "", "18",
                        "200", "190", "2000", "20", "400", "2400", "", "", "",
                        "GS", "22", "205", "2026-07-05", "210", "-10",
                        "", "", "",
                    ],
                ],
            },
        )
    finally:
        settings.google_target_sheet_name = old_sheet_name
        settings.google_target_header_row_count = old_header_count

    assert result["mode"] == "prepend_into_existing_sheet"
    assert result["sheet_name"] == "Накладная"
    assert result["block_start_row"] == 3
    assert result["block_end_row"] == 4
    assert result["separator_row"] == 5

    batch_update = fake_service.spreadsheets_resource.batch_updates[0]
    requests = batch_update["body"]["requests"]
    assert requests[0] == (
        {
            "insertDimension": {
                "range": {
                    "sheetId": 321,
                    "dimension": "ROWS",
                    "startIndex": 2,
                    "endIndex": 5,
                },
                "inheritFromBefore": False,
            }
        }
    )
    assert [request["copyPaste"]["pasteType"] for request in requests[1:]] == [
        "PASTE_FORMAT",
        "PASTE_DATA_VALIDATION",
        "PASTE_FORMULA",
    ]
    assert requests[-1]["copyPaste"]["source"]["startColumnIndex"] == 40

    value_update = fake_service.spreadsheets_resource.values_resource.updated[0]
    assert value_update["range"] == "Накладная!A3:AO5"
    written = value_update["body"]["values"]
    assert len(written) == 3
    assert len(written[0]) == 41
    assert written[0][16] == "item-1"
    assert written[0][20] == "10"
    assert written[0][21] == ""
    assert written[0][38] == "2026-07-03T10:00:00"
    assert written[0][39] == "doc-id-1"
    assert written[0][40] == "uploads/invoices/test.jpg"
    assert written[0][10] == "Shipper"
    assert written[1][16] == "item-2"
    assert written[1][2] == "Нет в справочнике"
    assert written[1][20] == "20"
    assert written[1][40] == ""
    assert written[2] == [""] * 41

    border_update = fake_service.spreadsheets_resource.batch_updates[1]
    border_requests = border_update["body"]["requests"]
    assert border_requests[0]["updateBorders"] == {
        "range": {
            "sheetId": 321,
            "startRowIndex": 3,
            "endRowIndex": 4,
            "startColumnIndex": 41,
            "endColumnIndex": 47,
        },
        "bottom": {"style": "NONE"},
    }
    assert border_requests[1]["updateBorders"]["range"] == {
        "sheetId": 321,
        "startRowIndex": 3,
        "endRowIndex": 4,
        "startColumnIndex": 0,
        "endColumnIndex": 41,
    }
    assert border_requests[1]["updateBorders"]["bottom"]["style"] == "SOLID"
