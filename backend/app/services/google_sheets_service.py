import json
import re
from typing import Any

from app.config import settings
from app.services.google_oauth_service import get_google_user_credentials

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

SHARED_INVOICE_HEADERS = [
    "Статус загрузки", "Статус строки", "Корректировка", "Дубль", "Форма документа",
    "Загрузка", "Дата документа", "№ Документа", "Поставщик", "ИНН Поставщика",
    "Грузоотправитель", "Получатель", "Торговая точка", "Склад", "Основание",
    "Товар найден в справочнике", "Наименование товара из документа",
    "Наименование товара в УС", "Ед.изм. в документе", "Ед.изм. в УС",
    "Кол-во в документе", "Кол-во в упаковке", "Кол-во в УС", "Цена за ед-цу", "Цена в УС",
    "Стоимость без НДС", "Ставка НДС", "Сумма НДС", "Общая стоимость",
    "Сумма накладной", "Дата приема", "Принял, Ф.И.О.", "Госсистемы",
    "Кол-во в заявке", "Цена по прайсу", "Предыдущая дата поставки",
    "Предыдущая цена", "Отклонение от цены прайса", "Время загрузки документа",
    "ID документа", "Ссылка на исходный документ",
]


INVOICE_REGISTER_COLUMN_WIDTHS = {
    0: 160,   # Статус строки
    1: 220,   # Корректировка
    2: 120,   # Дубль
    4: 130,   # Загрузка
    9: 220,   # Грузоотправитель
    14: 220,  # Товар найден в справочнике
    15: 280,  # Наименование товара из документа
    16: 240,  # Наименование товара в УС
    17: 190,  # Ед.изм. в документе
    20: 190,  # Кол-во в документе
    21: 180,  # Кол-во в упаковке
    23: 150,  # Цена за ед-цу
    25: 180,  # Стоимость без НДС
    26: 130,  # Ставка НДС
    32: 180,  # Госсистемы
    35: 240,  # Предыдущая дата поставки
    36: 190,  # Предыдущая цена
    37: 230,  # Отклонение от цены прайса
    38: 205,  # Время загрузки документа
    39: 130,  # ID документа
    40: 250,  # Ссылка на исходный документ
}

HEADER_BACKGROUND_COLOR = {
    "red": 53 / 255,
    "green": 104 / 255,
    "blue": 84 / 255,
}

FIRST_BAND_BACKGROUND_COLOR = {"red": 1, "green": 1, "blue": 1}
SECOND_BAND_BACKGROUND_COLOR = {
    "red": 243 / 255,
    "green": 245 / 255,
    "blue": 246 / 255,
}
DOCUMENT_SEPARATOR_BORDER_COLOR = {
    "red": 82 / 255,
    "green": 96 / 255,
    "blue": 105 / 255,
}
INVOICE_REGISTER_COLUMN_COUNT = 41


class GoogleSheetsConfigurationError(RuntimeError):
    pass


def load_invoice_reference_catalogs() -> dict[str, list[dict[str, Any]]]:
    spreadsheet_id = settings.google_target_spreadsheet_id
    if not settings.google_sheets_enabled or not spreadsheet_id:
        raise GoogleSheetsConfigurationError(
            "Для чтения справочников нужны GOOGLE_SHEETS_ENABLED=true и GOOGLE_TARGET_SPREADSHEET_ID."
        )
    sheets_service, _ = _build_google_services()
    ranges = ["'Товары'!A1:H", "'Поставщики'!A1:H", "'Справочник фасовок'!A1:M"]
    if settings.google_conversion_exceptions_sheet_name:
        escaped_name = settings.google_conversion_exceptions_sheet_name.replace("'", "''")
        ranges.append(f"'{escaped_name}'!A1:Z")
    response = sheets_service.spreadsheets().values().batchGet(
        spreadsheetId=spreadsheet_id,
        ranges=ranges,
        majorDimension="ROWS",
    ).execute()
    ranges = response.get("valueRanges") or []
    products_rows = ranges[0].get("values", []) if len(ranges) > 0 else []
    suppliers_rows = ranges[1].get("values", []) if len(ranges) > 1 else []
    packages_rows = ranges[2].get("values", []) if len(ranges) > 2 else []
    exceptions_rows = ranges[3].get("values", []) if len(ranges) > 3 else []
    return {
        "products": _confirmed_reference_rows(_table_rows_as_dicts(products_rows)),
        "suppliers": _confirmed_reference_rows(_table_rows_as_dicts(suppliers_rows)),
        "packages": _table_rows_as_dicts(packages_rows),
        "conversion_exceptions": _table_rows_as_dicts(exceptions_rows),
    }


def _confirmed_reference_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    confirmed_statuses = {"", "matched", "ready", "подтвержден", "подтверждено", "сопоставлено"}
    result = []
    for row in rows:
        status = _normalize_reference_sheet_name(
            row.get("Статус сопоставления") or row.get("Статус") or ""
        )
        if status in confirmed_statuses:
            result.append(row)
    return result


def create_invoice_review_spreadsheet(
    receiving,
    sheet_data: dict,
    apps_script_text: str | None = None,
    public_api_base_url: str | None = None,
    existing_spreadsheet_id: str | None = None,
) -> dict[str, Any]:
    """Create or update the Google Spreadsheet used for invoice review.

    If an invoice-register spreadsheet id is configured/passed, the new invoice
    rows are appended to that single spreadsheet. Otherwise a spreadsheet is
    created once and its id can be reused on the following uploads.
    """
    if not settings.google_sheets_enabled:
        raise GoogleSheetsConfigurationError(
            "Google Sheets API отключен. Укажите GOOGLE_SHEETS_ENABLED=true и credentials OAuth user."
        )
    sheets_service, drive_service = _build_google_services()

    target_spreadsheet_id = settings.google_target_spreadsheet_id
    if target_spreadsheet_id:
        return _insert_into_existing_spreadsheet(
            receiving=receiving,
            sheets_service=sheets_service,
            drive_service=drive_service,
            spreadsheet_id=target_spreadsheet_id,
            sheet_data=sheet_data,
        )

    spreadsheet_id = _resolve_spreadsheet_id(existing_spreadsheet_id)
    if spreadsheet_id:
        return _append_invoice_review_to_existing_spreadsheet(
            sheets_service=sheets_service,
            spreadsheet_id=spreadsheet_id,
            sheet_data=sheet_data,
        )

    return _create_invoice_review_spreadsheet(
        sheets_service=sheets_service,
        drive_service=drive_service,
        sheet_data=sheet_data,
    )


def _create_invoice_review_spreadsheet(sheets_service, drive_service, sheet_data: dict) -> dict[str, Any]:
    primary_sheet_name = sheet_data.get("primary_sheet_name") or next(iter(sheet_data["sheets"]))
    spreadsheet_body = {
        "properties": {"title": sheet_data["spreadsheet_name"]},
        "sheets": [
            {"properties": {"title": primary_sheet_name}},
        ],
    }
    spreadsheet = sheets_service.spreadsheets().create(body=spreadsheet_body, fields="spreadsheetId,spreadsheetUrl").execute()
    spreadsheet_id = spreadsheet["spreadsheetId"]
    spreadsheet_url = spreadsheet["spreadsheetUrl"]
    summary_values = sheet_data["sheets"][primary_sheet_name]
    values = [
        {"range": f"{primary_sheet_name}!A1:AM500", "values": summary_values},
    ]
    sheets_service.spreadsheets().values().batchUpdate(
        spreadsheetId=spreadsheet_id,
        body={"valueInputOption": "RAW", "data": values},
    ).execute()
    _format_spreadsheet(sheets_service, spreadsheet_id)
    sheet_id = _sheet_id_by_title(sheets_service, spreadsheet_id, primary_sheet_name)
    _add_invoice_bottom_separator(
        sheets_service=sheets_service,
        spreadsheet_id=spreadsheet_id,
        sheet_id=sheet_id,
        end_row=len(summary_values),
    )
    _move_spreadsheet_to_configured_folder(drive_service, spreadsheet_id)

    return _spreadsheet_result(
        spreadsheet_id=spreadsheet_id,
        spreadsheet_url=spreadsheet_url,
        spreadsheet_name=sheet_data["spreadsheet_name"],
        sheet_name=primary_sheet_name,
        data_start_row=2,
        data_end_row=max(2, len(summary_values)),
        mode="created",
    )


def _append_invoice_review_to_existing_spreadsheet(sheets_service, spreadsheet_id: str, sheet_data: dict) -> dict[str, Any]:
    primary_sheet_name = sheet_data.get("primary_sheet_name") or next(iter(sheet_data["sheets"]))
    spreadsheet = sheets_service.spreadsheets().get(
        spreadsheetId=spreadsheet_id,
        fields="spreadsheetId,spreadsheetUrl,properties.title,sheets.properties(sheetId,title,index)",
    ).execute()
    spreadsheet_url = spreadsheet.get("spreadsheetUrl") or _spreadsheet_url(spreadsheet_id)
    sheet_name = _ensure_register_sheet(sheets_service, spreadsheet_id, spreadsheet, primary_sheet_name)
    summary_values = sheet_data["sheets"][primary_sheet_name]
    header_row = summary_values[:1]
    data_rows = summary_values[1:]

    _ensure_header_row(sheets_service, spreadsheet_id, sheet_name, header_row)
    next_document_id = _next_document_id(sheets_service, spreadsheet_id, sheet_name)
    data_rows = _with_document_id(data_rows, next_document_id)

    if data_rows:
        start_row = 2
        end_row = start_row + len(data_rows) - 1
        sheet_id = _sheet_id_by_title(sheets_service, spreadsheet_id, sheet_name)
        _insert_rows_after_header(sheets_service, spreadsheet_id, sheet_id, len(data_rows))
        sheets_service.spreadsheets().values().update(
            spreadsheetId=spreadsheet_id,
            range=f"{sheet_name}!A{start_row}:AM{end_row}",
            valueInputOption="RAW",
            body={"values": data_rows},
        ).execute()
    else:
        start_row = 2
        end_row = 2

    _format_spreadsheet(sheets_service, spreadsheet_id)
    if data_rows:
        _add_invoice_bottom_separator(
            sheets_service=sheets_service,
            spreadsheet_id=spreadsheet_id,
            sheet_id=sheet_id,
            end_row=end_row,
        )

    return _spreadsheet_result(
        spreadsheet_id=spreadsheet_id,
        spreadsheet_url=spreadsheet_url,
        spreadsheet_name=sheet_data["spreadsheet_name"],
        sheet_name=sheet_name,
        data_start_row=start_row,
        data_end_row=end_row,
        mode="prepended",
    )


def _ensure_register_sheet(sheets_service, spreadsheet_id: str, spreadsheet: dict, sheet_name: str) -> str:
    sheets = spreadsheet.get("sheets", [])
    titles = [sheet["properties"]["title"] for sheet in sheets]
    if sheet_name in titles:
        return sheet_name

    if len(sheets) == 1:
        first_sheet = sheets[0]["properties"]
        first_title = first_sheet["title"]
        first_values = _get_values(sheets_service, spreadsheet_id, f"{first_title}!A1:AM2")
        if not first_values:
            sheets_service.spreadsheets().batchUpdate(
                spreadsheetId=spreadsheet_id,
                body={
                    "requests": [
                        {
                            "updateSheetProperties": {
                                "properties": {"sheetId": first_sheet["sheetId"], "title": sheet_name},
                                "fields": "title",
                            }
                        }
                    ]
                },
            ).execute()
            return sheet_name

    sheets_service.spreadsheets().batchUpdate(
        spreadsheetId=spreadsheet_id,
        body={"requests": [{"addSheet": {"properties": {"title": sheet_name}}}]},
    ).execute()
    return sheet_name


def _ensure_header_row(sheets_service, spreadsheet_id: str, sheet_name: str, header_row: list[list[Any]]) -> None:
    if not header_row:
        return

    existing_header = _get_values(sheets_service, spreadsheet_id, f"{sheet_name}!A1:AM1")
    expected_header = header_row[0]
    current_header = existing_header[0] if existing_header else []
    if current_header[:len(expected_header)] == expected_header:
        return

    sheets_service.spreadsheets().values().update(
        spreadsheetId=spreadsheet_id,
        range=f"{sheet_name}!A1:AM1",
        valueInputOption="RAW",
        body={"values": header_row},
    ).execute()


def _next_document_id(sheets_service, spreadsheet_id: str, sheet_name: str) -> int:
    # В новой шапке ID документа находится в колонке AL. Колонку B тоже
    # читаем как fallback для таблиц, созданных до переноса служебных полей.
    document_id_values = _get_values(sheets_service, spreadsheet_id, f"{sheet_name}!AL2:AL")
    legacy_document_id_values = _get_values(sheets_service, spreadsheet_id, f"{sheet_name}!B2:B")
    existing_ids = [
        _parse_document_id(row[0])
        for row in document_id_values + legacy_document_id_values
        if row
    ]
    numeric_ids = [document_id for document_id in existing_ids if document_id is not None]
    if numeric_ids:
        return max(numeric_ids) + 1
    return 1


def _parse_document_id(value: Any) -> int | None:
    document_id = None
    if isinstance(value, int):
        document_id = value
    elif isinstance(value, float) and value.is_integer():
        document_id = int(value)
    elif isinstance(value, str):
        text = value.strip()
        if re.fullmatch(r"\d+", text):
            document_id = int(text)
    return document_id


def _with_document_id(data_rows: list[list[Any]], document_id: int) -> list[list[Any]]:
    updated_rows = [list(row) for row in data_rows]
    if updated_rows:
        first_row = updated_rows[0]
        while len(first_row) < 38:
            first_row.append("")
        first_row[37] = document_id
    return updated_rows


def _sheet_id_by_title(sheets_service, spreadsheet_id: str, sheet_name: str) -> int:
    spreadsheet = sheets_service.spreadsheets().get(
        spreadsheetId=spreadsheet_id,
        fields="sheets.properties(sheetId,title)",
    ).execute()
    for sheet in spreadsheet.get("sheets", []):
        properties = sheet.get("properties", {})
        if properties.get("title") == sheet_name:
            return properties["sheetId"]
    raise GoogleSheetsConfigurationError(f"Лист '{sheet_name}' не найден в Google Таблице.")


def _insert_rows_after_header(sheets_service, spreadsheet_id: str, sheet_id: int, row_count: int) -> None:
    if row_count <= 0:
        return
    sheets_service.spreadsheets().batchUpdate(
        spreadsheetId=spreadsheet_id,
        body={
            "requests": [
                {
                    "insertDimension": {
                        "range": {
                            "sheetId": sheet_id,
                            "dimension": "ROWS",
                            "startIndex": 1,
                            "endIndex": 1 + row_count,
                        },
                        "inheritFromBefore": False,
                    }
                }
            ]
        },
    ).execute()


def _add_invoice_bottom_separator(
    sheets_service,
    spreadsheet_id: str,
    sheet_id: int,
    end_row: int,
    end_column_index: int | None = None,
    clear_to_column_index: int | None = None,
) -> None:
    if end_row < 2:
        return

    separator_end = end_column_index or INVOICE_REGISTER_COLUMN_COUNT
    requests = []
    if clear_to_column_index and clear_to_column_index > separator_end:
        requests.append(
            {
                "updateBorders": {
                    "range": {
                        "sheetId": sheet_id,
                        "startRowIndex": end_row - 1,
                        "endRowIndex": end_row,
                        "startColumnIndex": separator_end,
                        "endColumnIndex": clear_to_column_index,
                    },
                    "bottom": {"style": "NONE"},
                }
            }
        )
    requests.append(
        {
            "updateBorders": {
                "range": {
                    "sheetId": sheet_id,
                    "startRowIndex": end_row - 1,
                    "endRowIndex": end_row,
                    "startColumnIndex": 0,
                    "endColumnIndex": separator_end,
                },
                "bottom": {
                    "style": "SOLID",
                    "color": DOCUMENT_SEPARATOR_BORDER_COLOR,
                },
            }
        }
    )
    sheets_service.spreadsheets().batchUpdate(
        spreadsheetId=spreadsheet_id,
        body={"requests": requests},
    ).execute()


def _get_values(sheets_service, spreadsheet_id: str, range_name: str) -> list[list[Any]]:
    result = sheets_service.spreadsheets().values().get(spreadsheetId=spreadsheet_id, range=range_name).execute()
    return result.get("values", [])


def _move_spreadsheet_to_configured_folder(drive_service, spreadsheet_id: str) -> None:
    if settings.google_drive_folder_id:
        drive_service.files().update(
            fileId=spreadsheet_id,
            addParents=settings.google_drive_folder_id,
            fields="id, parents",
            supportsAllDrives=True,
        ).execute()


def _spreadsheet_result(
    spreadsheet_id: str,
    spreadsheet_url: str,
    spreadsheet_name: str,
    sheet_name: str,
    data_start_row: int,
    data_end_row: int,
    mode: str,
) -> dict[str, Any]:
    button_result = {
        "installed": False,
        "status": "button_removed",
        "send_page_url": None,
        "message": "Кнопка-ссылка 'Отправить в iiko' на листе 'Накладные' не создаётся.",
    }
    return {
        "spreadsheet_id": spreadsheet_id,
        "spreadsheet_url": spreadsheet_url,
        "spreadsheet_name": spreadsheet_name,
        "sheet_name": sheet_name,
        "data_start_row": data_start_row,
        "data_end_row": data_end_row,
        "data_range": f"{sheet_name}!A{data_start_row}:AM{data_end_row}",
        "mode": mode,
        "send_button": button_result,
    }


def _build_google_services():
    try:
        from googleapiclient.discovery import build
    except ImportError as exc:
        raise GoogleSheetsConfigurationError(
            "Не установлены зависимости google-api-python-client/google-auth/google-auth-oauthlib. Выполните pip install -r requirements.txt."
        ) from exc
    credentials = get_google_user_credentials()
    return (
        build("sheets", "v4", credentials=credentials),
        build("drive", "v3", credentials=credentials),
    )


def _table_rows_as_dicts(rows: list[list[Any]]) -> list[dict[str, Any]]:
    if not rows:
        return []
    headers = [str(value).strip() for value in rows[0]]
    result = []
    for row in rows[1:]:
        mapped = {
            header: row[index] if index < len(row) else ""
            for index, header in enumerate(headers)
            if header
        }
        if any(value not in (None, "") for value in mapped.values()):
            result.append(mapped)
    return result




def _format_spreadsheet(sheets_service, spreadsheet_id: str) -> None:
    spreadsheet = sheets_service.spreadsheets().get(spreadsheetId=spreadsheet_id).execute()
    sheet_ids = {sheet["properties"]["title"]: sheet["properties"]["sheetId"] for sheet in spreadsheet["sheets"]}
    requests = []
    for title, sheet_id in sheet_ids.items():
        header_end_column_index = INVOICE_REGISTER_COLUMN_COUNT if title == "Накладные" else (7 if title == "Накладная" else 10)
        requests.append(
            {
                "updateSheetProperties": {
                    "properties": {
                        "sheetId": sheet_id,
                        "gridProperties": {"frozenRowCount": 1},
                    },
                    "fields": "gridProperties.frozenRowCount",
                }
            }
        )
        _delete_sheet_bandings(spreadsheet, requests, sheet_id)
        requests.append(
            {
                "addBanding": {
                    "bandedRange": {
                        "range": {
                            "sheetId": sheet_id,
                            "startRowIndex": 0,
                            "endRowIndex": 500,
                            "startColumnIndex": 0,
                            "endColumnIndex": header_end_column_index,
                        },
                        "rowProperties": {
                            "headerColor": HEADER_BACKGROUND_COLOR,
                            "firstBandColor": FIRST_BAND_BACKGROUND_COLOR,
                            "secondBandColor": SECOND_BAND_BACKGROUND_COLOR,
                        },
                    }
                }
            }
        )
        requests.append(
            {
                "repeatCell": {
                    "range": {
                        "sheetId": sheet_id,
                        "startRowIndex": 0,
                        "endRowIndex": 1,
                        "startColumnIndex": 0,
                        "endColumnIndex": header_end_column_index,
                    },
                    "cell": {
                        "userEnteredFormat": {
                            "backgroundColor": HEADER_BACKGROUND_COLOR,
                            "horizontalAlignment": "LEFT",
                            "verticalAlignment": "MIDDLE",
                            "wrapStrategy": "WRAP",
                            "textFormat": {
                                "bold": True,
                                "foregroundColor": {"red": 1, "green": 1, "blue": 1},
                            },
                        }
                    },
                    "fields": "userEnteredFormat(backgroundColor,horizontalAlignment,verticalAlignment,wrapStrategy,textFormat)",
                }
            }
        )
        requests.append(
            {
                "repeatCell": {
                    "range": {
                        "sheetId": sheet_id,
                        "startRowIndex": 1,
                        "endRowIndex": 500,
                        "startColumnIndex": 0,
                        "endColumnIndex": header_end_column_index,
                    },
                    "cell": {"userEnteredFormat": {"wrapStrategy": "CLIP", "verticalAlignment": "MIDDLE"}},
                    "fields": "userEnteredFormat(wrapStrategy,verticalAlignment)",
                }
            }
        )
        requests.append(
            {
                "updateDimensionProperties": {
                    "range": {
                        "sheetId": sheet_id,
                        "dimension": "COLUMNS",
                        "startIndex": 0,
                        "endIndex": header_end_column_index,
                    },
                    "properties": {"pixelSize": 145},
                    "fields": "pixelSize",
                }
            }
        )
        if title == "Накладные":
            for column_index, pixel_size in INVOICE_REGISTER_COLUMN_WIDTHS.items():
                requests.append(
                    {
                        "updateDimensionProperties": {
                            "range": {
                                "sheetId": sheet_id,
                                "dimension": "COLUMNS",
                                "startIndex": column_index,
                                "endIndex": column_index + 1,
                            },
                            "properties": {"pixelSize": pixel_size},
                            "fields": "pixelSize",
                        }
                    }
                )
        requests.append(
            {
                "updateDimensionProperties": {
                    "range": {
                        "sheetId": sheet_id,
                        "dimension": "ROWS",
                        "startIndex": 0,
                        "endIndex": 1,
                    },
                    "properties": {"pixelSize": 36},
                    "fields": "pixelSize",
                }
            }
        )
        requests.append(
            {
                "setBasicFilter": {
                    "filter": {
                        "range": {
                            "sheetId": sheet_id,
                            "startRowIndex": 0,
                            "startColumnIndex": 0,
                            "endColumnIndex": header_end_column_index,
                        }
                    }
                }
            }
        )
    if requests:
        sheets_service.spreadsheets().batchUpdate(spreadsheetId=spreadsheet_id, body={"requests": requests}).execute()


def _insert_into_existing_spreadsheet(
    *,
    receiving,
    sheets_service,
    drive_service,
    spreadsheet_id: str,
    sheet_data: dict,
) -> dict[str, Any]:
    spreadsheet = sheets_service.spreadsheets().get(
        spreadsheetId=spreadsheet_id,
        fields="spreadsheetUrl,sheets.properties(sheetId,title,gridProperties(rowCount,columnCount))",
    ).execute()
    spreadsheet_url = spreadsheet["spreadsheetUrl"]
    target_sheet_name = settings.google_target_sheet_name
    target_sheet = None
    for sheet in spreadsheet.get("sheets", []):
        props = sheet.get("properties", {})
        if props.get("title") == target_sheet_name:
            target_sheet = props
            break
    if target_sheet is None:
        raise GoogleSheetsConfigurationError(
            f"В target spreadsheet не найден лист '{target_sheet_name}'."
        )

    source_sheet_name = sheet_data.get("primary_sheet_name") or next(iter(sheet_data["sheets"]))
    source_rows = sheet_data["sheets"].get(source_sheet_name) or []
    shared_rows = sheet_data.get("shared_sheet_rows") or []
    if not source_rows and not shared_rows:
        raise GoogleSheetsConfigurationError("Нет данных для записи в target spreadsheet.")

    # In a shared operator sheet the real machine-bound header already exists.
    # New documents are inserted immediately under that header, newest first.
    target_headers = _read_target_headers(
        sheets_service,
        spreadsheet_id,
        target_sheet_name,
        settings.google_target_header_row_count,
    )
    receiving_id = getattr(receiving, "id", None)
    existing_row = (
        _find_existing_document_row(
            sheets_service,
            spreadsheet_id,
            target_sheet_name,
            target_headers,
            receiving_id,
            settings.google_target_header_row_count,
        )
        if receiving_id is not None
        else None
    )
    if existing_row is not None:
        return {
            "spreadsheet_id": spreadsheet_id,
            "spreadsheet_url": spreadsheet_url,
            "spreadsheet_name": sheet_data["spreadsheet_name"],
            "sheet_name": target_sheet_name,
            "header_row_count": max(settings.google_target_header_row_count, 1),
            "block_start_row": existing_row,
            "block_end_row": existing_row,
            "separator_row": None,
            "mode": "already_exists",
            "send_button": {
                "installed": False,
                "status": "button_removed",
                "send_page_url": None,
                "message": "Документ уже присутствует в общем реестре.",
            },
        }
    document_rows = (
        _align_shared_rows_to_target_headers(shared_rows, target_headers)
        if shared_rows
        else _remap_source_rows_to_shared_sheet(source_rows, receiving, target_headers)
    )
    if not document_rows:
        template_width = len(target_headers) if target_headers else len(source_rows[0])
        document_rows = [[""] * template_width]
    column_count = max(len(row) for row in document_rows)
    separator_row = [""] * column_count
    rows_to_insert = document_rows + [separator_row]

    header_row_count = max(settings.google_target_header_row_count, 1)
    insert_start_index = header_row_count
    insert_end_index = insert_start_index + len(rows_to_insert)
    first_document_row_number = insert_start_index + 1
    last_document_row_number = first_document_row_number + len(document_rows) - 1

    requests = [
        {
            "insertDimension": {
                "range": {
                    "sheetId": target_sheet["sheetId"],
                    "dimension": "ROWS",
                    "startIndex": insert_start_index,
                    "endIndex": insert_end_index,
                },
                "inheritFromBefore": False,
            }
        }
    ]
    target_column_count = max(
        (target_sheet.get("gridProperties") or {}).get("columnCount", 0),
        len(target_headers),
    )
    border_column_count = _invoice_separator_column_count(
        target_column_count=target_column_count,
        document_column_count=column_count,
        target_headers=target_headers,
    )
    formula_start_column = len(SHARED_INVOICE_HEADERS)
    if target_column_count == 47:
        formula_start_column -= 1
    if target_column_count >= 47:
        template_row_start = insert_end_index
        inserted_rows = {
            "sheetId": target_sheet["sheetId"],
            "startRowIndex": insert_start_index,
            "endRowIndex": insert_end_index,
        }
        template_row = {
            "sheetId": target_sheet["sheetId"],
            "startRowIndex": template_row_start,
            "endRowIndex": template_row_start + 1,
        }
        for paste_type in ("PASTE_FORMAT", "PASTE_DATA_VALIDATION"):
            requests.append(
                {
                    "copyPaste": {
                        "source": {**template_row, "startColumnIndex": 0, "endColumnIndex": target_column_count},
                        "destination": {**inserted_rows, "startColumnIndex": 0, "endColumnIndex": target_column_count},
                        "pasteType": paste_type,
                        "pasteOrientation": "NORMAL",
                    }
                }
            )
        requests.append(
            {
                "copyPaste": {
                    "source": {**template_row, "startColumnIndex": formula_start_column, "endColumnIndex": target_column_count},
                    "destination": {**inserted_rows, "startColumnIndex": formula_start_column, "endColumnIndex": target_column_count},
                    "pasteType": "PASTE_FORMULA",
                    "pasteOrientation": "NORMAL",
                }
            }
        )
    sheets_service.spreadsheets().batchUpdate(
        spreadsheetId=spreadsheet_id,
        body={"requests": requests},
    ).execute()

    target_range = f"{target_sheet_name}!A{first_document_row_number}:{_column_index_to_a1(column_count - 1)}{insert_end_index}"
    sheets_service.spreadsheets().values().update(
        spreadsheetId=spreadsheet_id,
        range=target_range,
        valueInputOption="RAW",
        body={"values": rows_to_insert},
    ).execute()
    _add_invoice_bottom_separator(
        sheets_service=sheets_service,
        spreadsheet_id=spreadsheet_id,
        sheet_id=target_sheet["sheetId"],
        end_row=last_document_row_number,
        end_column_index=border_column_count,
        clear_to_column_index=target_column_count,
    )

    return {
        "spreadsheet_id": spreadsheet_id,
        "spreadsheet_url": spreadsheet_url,
        "spreadsheet_name": sheet_data["spreadsheet_name"],
        "sheet_name": target_sheet_name,
        "header_row_count": header_row_count,
        "block_start_row": first_document_row_number,
        "block_end_row": last_document_row_number,
        "separator_row": last_document_row_number + 1,
        "mode": "prepend_into_existing_sheet",
        "send_button": {
            "installed": False,
            "status": "button_removed",
            "send_page_url": None,
            "message": "Кнопка-ссылка 'Отправить в iiko' на листе общего реестра не создаётся.",
        },
    }



def _find_existing_document_row(
    sheets_service,
    spreadsheet_id: str,
    sheet_name: str,
    headers: list[str],
    document_id: int | str,
    header_row_count: int,
) -> int | None:
    try:
        column_index = headers.index("ID документа")
    except ValueError:
        return None
    values_resource = sheets_service.spreadsheets().values()
    if not hasattr(values_resource, "get"):
        return None
    column = _column_index_to_a1(column_index)
    start_row = max(1, header_row_count) + 1
    try:
        response = values_resource.get(
            spreadsheetId=spreadsheet_id,
            range=f"{sheet_name}!{column}{start_row}:{column}",
        ).execute()
    except Exception:  # noqa: BLE001 - deduplication must not block a valid write
        return None
    expected = str(document_id)
    for offset, row in enumerate(response.get("values") or []):
        if row and str(row[0]).strip() == expected:
            return start_row + offset
    return None

def _invoice_separator_column_count(
    *,
    target_column_count: int,
    document_column_count: int,
    target_headers: list[str],
) -> int:
    table_column_count = max(document_column_count, len(target_headers), INVOICE_REGISTER_COLUMN_COUNT)
    if target_column_count > 0:
        table_column_count = min(table_column_count, target_column_count)
    return max(table_column_count, 1)


def _read_target_headers(
    sheets_service,
    spreadsheet_id: str,
    sheet_name: str,
    header_row_number: int,
) -> list[str]:
    values_resource = sheets_service.spreadsheets().values()
    if not hasattr(values_resource, "get"):
        return SHARED_INVOICE_HEADERS
    headers = _fetch_target_headers(
        values_resource,
        spreadsheet_id,
        sheet_name,
        header_row_number,
    )
    if not headers:
        raise GoogleSheetsConfigurationError(f"Строка заголовков {header_row_number} листа '{sheet_name}' пуста.")

    missing = [header for header in SHARED_INVOICE_HEADERS if header not in headers]
    if missing == ["Кол-во в упаковке"]:
        _insert_units_per_package_column(
            sheets_service,
            values_resource,
            spreadsheet_id,
            sheet_name,
            header_row_number,
        )
        headers = _fetch_target_headers(
            values_resource,
            spreadsheet_id,
            sheet_name,
            header_row_number,
        )
        missing = [header for header in SHARED_INVOICE_HEADERS if header not in headers]
    if missing:
        raise GoogleSheetsConfigurationError(
            "В листе 'Накладная' отсутствуют обязательные заголовки: " + ", ".join(missing)
        )
    return headers


def _fetch_target_headers(
    values_resource,
    spreadsheet_id: str,
    sheet_name: str,
    header_row_number: int,
) -> list[str]:
    response = values_resource.get(
        spreadsheetId=spreadsheet_id,
        range=f"{sheet_name}!A{header_row_number}:AZ{header_row_number}",
    ).execute()
    rows = response.get("values") or []
    return [str(value).strip() for value in (rows[0] if rows else [])]


def _insert_units_per_package_column(
    sheets_service,
    values_resource,
    spreadsheet_id: str,
    sheet_name: str,
    header_row_number: int,
) -> None:
    spreadsheet = sheets_service.spreadsheets().get(
        spreadsheetId=spreadsheet_id,
        includeGridData=False,
    ).execute()
    sheet_id = next(
        (
            sheet.get("properties", {}).get("sheetId")
            for sheet in spreadsheet.get("sheets", [])
            if sheet.get("properties", {}).get("title") == sheet_name
        ),
        None,
    )
    if sheet_id is None:
        raise GoogleSheetsConfigurationError(f"В target spreadsheet не найден лист '{sheet_name}'.")

    column_index = SHARED_INVOICE_HEADERS.index("Кол-во в упаковке")
    sheets_service.spreadsheets().batchUpdate(
        spreadsheetId=spreadsheet_id,
        body={
            "requests": [
                {
                    "insertDimension": {
                        "range": {
                            "sheetId": sheet_id,
                            "dimension": "COLUMNS",
                            "startIndex": column_index,
                            "endIndex": column_index + 1,
                        },
                        "inheritFromBefore": False,
                    }
                },
                {
                    "copyPaste": {
                        "source": {
                            "sheetId": sheet_id,
                            "startColumnIndex": column_index - 1,
                            "endColumnIndex": column_index,
                        },
                        "destination": {
                            "sheetId": sheet_id,
                            "startColumnIndex": column_index,
                            "endColumnIndex": column_index + 1,
                        },
                        "pasteType": "PASTE_FORMAT",
                        "pasteOrientation": "NORMAL",
                    }
                },
            ]
        },
    ).execute()
    column_name = _column_index_to_a1(column_index)
    values_resource.update(
        spreadsheetId=spreadsheet_id,
        range=f"{sheet_name}!{column_name}{header_row_number}",
        valueInputOption="RAW",
        body={"values": [["Кол-во в упаковке"]]},
    ).execute()


def _remap_source_rows_to_shared_sheet(
    source_rows: list[list[Any]],
    receiving,
    target_headers: list[str] | None = None,
) -> list[list[Any]]:
    if not source_rows:
        return []
    source_headers = [str(cell).strip() for cell in source_rows[0]]
    target_headers = target_headers or SHARED_INVOICE_HEADERS
    target_width = len(target_headers)
    target_indexes = {header: index for index, header in enumerate(target_headers)}
    document_meta = _shared_sheet_document_meta(receiving)
    result: list[list[Any]] = []
    for index, row in enumerate(source_rows[1:], start=1):
        row_map = {
            header: row[column_index] if column_index < len(row) else ""
            for column_index, header in enumerate(source_headers)
            if header
        }
        target_row = [""] * target_width
        product_found = row_map.get("Товар найден в справочнике", "")
        correction = row_map.get("Причина ручной корректировки", "")
        if product_found == "Нет":
            correction = "Нет в справочнике"
        elif product_found == "?" and correction in ("", "Другое"):
            correction = "Сопоставление"
        mapped_values = {
            "Статус загрузки": row_map.get("Статус загрузки", ""),
            "Статус строки": row_map.get("Статус строки", ""),
            "Корректировка": correction,
            "Дубль": row_map.get("Индикатор дубля документа", ""),
            "Форма документа": document_meta.get("document_form") or row_map.get("Форма документа", ""),
            "Загрузка": row_map.get("Загрузить в УС", ""),
            "Дата документа": row_map.get("Дата документа", ""),
            "№ Документа": row_map.get("№ Документа", ""),
            "Поставщик": row_map.get("Поставщик", ""),
            "ИНН Поставщика": document_meta.get("supplier_inn") or row_map.get("ИНН Поставщика", ""),
            "Грузоотправитель": document_meta.get("shipper") or row_map.get("Грузоотправитель", ""),
            "Получатель": document_meta.get("recipient") or row_map.get("Получатель", ""),
            "Торговая точка": row_map.get("Торговая точка", ""),
            "Склад": row_map.get("Склад", ""),
            "Основание": document_meta.get("basis") or row_map.get("Основание", ""),
            "Товар найден в справочнике": product_found,
            "Наименование товара из документа": row_map.get("Наименование товара из документа", ""),
            "Наименование товара в УС": row_map.get("Наименование товара в УС", ""),
            "Ед.изм. в документе": row_map.get("Ед.изм.", ""),
            "Ед.изм. в УС": row_map.get("Ед.изм. в УС", ""),
            "Кол-во в документе": row_map.get("Кол-во из документа", ""),
            "Кол-во в упаковке": row_map.get("Кол-во в упаковке", ""),
            "Кол-во в УС": row_map.get("Кол-во в УС", ""),
            "Цена за ед-цу": row_map.get("Цена за единицу", ""),
            "Цена в УС": row_map.get("Цена в УС", ""),
            "Стоимость без НДС": row_map.get("Стоимость без НДС", ""),
            "Ставка НДС": row_map.get("Ставка НДС %", ""),
            "Сумма НДС": row_map.get("Сумма НДС", ""),
            "Общая стоимость": row_map.get("Общая стоимость", ""),
            "Сумма накладной": row_map.get("Сумма накладной", ""),
            "Дата приема": row_map.get("Дата приема", ""),
            "Принял, Ф.И.О.": row_map.get("Принял, Ф.И.О.", ""),
            "Госсистемы": row_map.get("Госсистемы", ""),
            "Кол-во в заявке": row_map.get("Кол-во в заявке", ""),
            "Цена по прайсу": row_map.get("Цена по прайсу", ""),
            "Предыдущая дата поставки": row_map.get("Последняя дата поставки", ""),
            "Предыдущая цена": row_map.get("Последняя цена", ""),
            "Отклонение от цены прайса": row_map.get("Отклонение от цены прайса", ""),
            "Время загрузки документа": row_map.get("Время загрузки документа", ""),
            "ID документа": row_map.get("ID документа", ""),
            "Ссылка на исходный документ": (
                (getattr(receiving, "documents", None) and getattr(receiving.documents[-1], "file_url", ""))
                or ""
            ),
        }
        first_row_only = {
            "Статус загрузки", "Статус строки", "Дубль", "Форма документа", "Дата документа",
            "№ Документа", "Поставщик", "ИНН Поставщика", "Грузоотправитель", "Получатель",
            "Торговая точка", "Склад", "Основание", "Сумма накладной",
            "Время загрузки документа", "ID документа", "Ссылка на исходный документ",
        }
        for header, value in mapped_values.items():
            if header in target_indexes:
                target_row[target_indexes[header]] = "" if index != 1 and header in first_row_only else value
        result.append(target_row)
    return result


def _align_shared_rows_to_target_headers(
    source_rows: list[list[Any]],
    target_headers: list[str] | None = None,
) -> list[list[Any]]:
    if not source_rows:
        return []
    target_headers = target_headers or SHARED_INVOICE_HEADERS
    target_width = len(target_headers)
    result: list[list[Any]] = []
    for row in source_rows:
        if len(row) == target_width:
            result.append(list(row))
            continue
        padded = list(row[:target_width])
        if len(padded) < target_width:
            padded.extend([""] * (target_width - len(padded)))
        result.append(padded)
    return result


def _shared_sheet_document_meta(receiving: Any) -> dict[str, Any]:
    documents = getattr(receiving, "documents", None) or []
    document = documents[-1] if documents else None
    raw_json = getattr(document, "recognized_items_json", None)
    if not raw_json:
        return {}
    try:
        payload = json.loads(raw_json)
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    header = payload.get("header") or {}
    return {
        "document_form": header.get("document_form") or "",
        "supplier_inn": header.get("supplier_inn") or "",
        "shipper": header.get("shipper") or "",
        "recipient": header.get("recipient") or "",
        "basis": header.get("basis") or "",
    }


def _column_index_to_a1(column_index: int) -> str:
    if column_index < 0:
        raise ValueError("column_index must be non-negative")
    result = ""
    index = column_index + 1
    while index:
        index, remainder = divmod(index - 1, 26)
        result = chr(ord("A") + remainder) + result
    return result


def serialize_sheet_result(result: dict[str, Any]) -> str:
    return json.dumps(result, ensure_ascii=False)


def sync_incremental_reference_catalogs(entries: list[dict[str, Any]]) -> dict[str, int]:
    """Append newly encountered products/suppliers to MVP reference tabs."""
    result = {"products": 0, "suppliers": 0}
    if not entries or not settings.google_sheets_enabled or not settings.google_target_spreadsheet_id:
        return result
    sheets_service, _ = _build_google_services()
    grouped = {
        "product": ("Товары", "products", ["Статус сопоставления", "Уверенность", "Источник", "Наименование из документа"]),
        "supplier": ("Поставщики", "suppliers", ["Статус сопоставления", "Уверенность", "Источник", "Наименование из документа"]),
    }
    for kind, (sheet_name, result_key, extra_headers) in grouped.items():
        kind_entries = [entry for entry in entries if entry.get("kind") == kind]
        if not kind_entries:
            continue
        result[result_key] = _append_reference_entries(
            sheets_service,
            settings.google_target_spreadsheet_id,
            sheet_name,
            kind_entries,
            extra_headers,
        )
    return result


def _append_reference_entries(
    sheets_service,
    spreadsheet_id: str,
    sheet_name: str,
    entries: list[dict[str, Any]],
    extra_headers: list[str],
) -> int:
    escaped_name = sheet_name.replace("'", "''")
    response = sheets_service.spreadsheets().values().get(
        spreadsheetId=spreadsheet_id,
        range=f"'{escaped_name}'!A1:Z",
        majorDimension="ROWS",
    ).execute()
    rows = response.get("values") or []
    if not rows:
        return 0
    headers = [str(value).strip() for value in rows[0]]
    headers = _ensure_reference_headers(
        sheets_service,
        spreadsheet_id,
        escaped_name,
        headers,
        extra_headers,
    )
    existing = {
        _normalize_reference_sheet_name(value)
        for row in rows[1:]
        for value in row[:2]
        if value not in (None, "")
    }
    new_rows = []
    for entry in entries:
        names = [entry.get("raw_name"), entry.get("external_name")]
        if any(_normalize_reference_sheet_name(name) in existing for name in names if name):
            continue
        new_rows.append([_reference_sheet_value(header, entry) for header in headers])
        existing.update(_normalize_reference_sheet_name(name) for name in names if name)
    if not new_rows:
        return 0
    sheets_service.spreadsheets().values().append(
        spreadsheetId=spreadsheet_id,
        range=f"'{escaped_name}'!A:Z",
        valueInputOption="RAW",
        insertDataOption="INSERT_ROWS",
        body={"values": new_rows},
    ).execute()
    return len(new_rows)


def _ensure_reference_headers(
    sheets_service,
    spreadsheet_id: str,
    escaped_sheet_name: str,
    headers: list[str],
    extra_headers: list[str],
) -> list[str]:
    normalized = {_normalize_reference_sheet_name(header) for header in headers}
    missing = [header for header in extra_headers if _normalize_reference_sheet_name(header) not in normalized]
    if not missing:
        return headers
    updated = headers + missing
    end_column = _column_index_to_a1(len(updated) - 1)
    sheets_service.spreadsheets().values().update(
        spreadsheetId=spreadsheet_id,
        range=f"'{escaped_sheet_name}'!A1:{end_column}1",
        valueInputOption="RAW",
        body={"values": [updated]},
    ).execute()
    return updated


def _reference_sheet_value(header: str, entry: dict[str, Any]) -> Any:
    normalized = _normalize_reference_sheet_name(header)
    status = entry.get("status") or ""
    unresolved = status in {"new", "needs_review"}
    if normalized in {"наименование", "товар", "наименование товара", "поставщик", "наименование поставщика"}:
        if unresolved:
            return entry.get("raw_name") or ""
        return entry.get("external_name") or entry.get("raw_name") or ""
    if normalized in {"код", "id", "внешний id", "external id", "код товара", "код поставщика"}:
        return "" if unresolved else entry.get("external_id") or ""
    if normalized in {"ед изм", "единица измерения", "единица"}:
        return entry.get("unit") or ""
    if normalized in {"статус", "статус сопоставления"}:
        return status
    if normalized in {"уверенность", "confidence"}:
        return entry.get("confidence") or 0
    if normalized in {"источник", "source"}:
        return entry.get("source") or ""
    if normalized in {"наименование из документа", "исходное наименование"}:
        return entry.get("raw_name") or ""
    return ""


def _normalize_reference_sheet_name(value: Any) -> str:
    text = str(value or "").lower().replace("ё", "е")
    text = re.sub(r"[^a-zа-я0-9]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()
