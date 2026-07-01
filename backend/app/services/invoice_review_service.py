import csv
import io
import json
import re
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from app.models.accounting import AccountingExport
from app.models.receiving import Receiving, ReceivingDocument, ReceivingItem, ReceivingItemStatus, ReceivingStatus
from app.services.google_sheets_service import create_invoice_review_spreadsheet, serialize_sheet_result
from app.services.iiko_incoming_invoice_service import build_iiko_export_payload, build_incoming_invoice_xml
from app.services.iiko_reference_mapping_service import auto_fill_iiko_fields, get_iiko_reference_context, invalidate_iiko_reference_cache

REQUIRED_FIELDS = {
    "supplier": "поставщик",
    "invoice_date": "дата накладной",
    "invoice_number": "номер накладной",
    "venue": "заведение / точка доставки",
    "items": "товарные позиции",
}

INVOICE_REGISTER_SHEET_NAME = "Накладные"
INVOICE_REGISTER_SPREADSHEET_NAME = "АвтоСнаб Накладные"
INVOICE_REGISTER_HEADERS = [
    "Время загрузки документа",
    "ID документа",
    "Индикатор дубля документа",
    "Форма документа",
    "Дата документа",
    "№ Документа",
    "Поставщик",
    "ИНН Поставщика",
    "Грузополучатель",
    "Получатель",
    "Торговая точка",
    "Склад",
    "Основание",
    "Наименование товара из документа",
    "Госсистемы",
    "Наименование товара в УС",
    "Товар найден в справочнике",
    "Ед.изм.",
    "Кол-во из документа",
    "Цена за единицу",
    "Стоимость без НДС",
    "Ставка НДС %",
    "Сумма НДС",
    "Общая стоимость",
    "Сумма накладной",
    "Ед.изм. в УС",
    "Кол-во в УС",
    "Цена в УС",
    "Дата приема",
    "Принял, Ф.И.О.",
    "Кол-во в заявке",
    "Цена по прайсу",
    "Последняя дата поставки",
    "Последняя цена",
    "Отклонение от цены прайса",
    "Загрузить в УС",
    "Статус строки",
    "Причина ручной корректировки",
]


def create_invoice_review(db: Session, payload) -> Receiving:
    supplier = _clean(payload.supplier) or ""
    venue = (
        _clean(getattr(payload, "venue", None))
        or _clean(getattr(payload, "trade_point", None))
        or _clean(getattr(payload, "iiko_organization", None))
        or ""
    )
    invoice_number = _clean(payload.invoice_number)
    internal_order_number = invoice_number or f"MANUAL-{datetime.utcnow().strftime('%Y%m%d%H%M%S')}"
    request_id = payload.request_id or f"MVP4-{internal_order_number}"

    receiving = Receiving(
        request_id=request_id,
        order_number=internal_order_number,
        venue=venue,
        supplier=supplier,
        delivery_address=payload.delivery_address,
        chat_id=payload.chat_id,
        user_id=payload.user_id,
        status=ReceivingStatus.ocr_processed,
        comment="MVP-4: накладная загружена для ручной проверки перед отправкой в iiko",
    )
    db.add(receiving)
    db.flush()

    recognized_items = [_item_payload(item, index) for index, item in enumerate(payload.items, start=1)]
    header_meta = _header_payload(payload)
    mapping_result = auto_fill_iiko_fields(header_meta, recognized_items, supplier_name=supplier, venue=venue)
    header_meta = mapping_result["header"]
    recognized_items = mapping_result["items"]
    if mapping_result.get("notes"):
        header_meta["mapping_notes"] = mapping_result["notes"]
    document = ReceivingDocument(
        receiving_id=receiving.id,
        file_id=payload.file_id,
        file_type=payload.file_type,
        source="invoice_review_mvp4",
        file_url=payload.file_url,
        ocr_status="ocr_processed" if recognized_items else "manual_review",
        raw_text=payload.raw_text,
        recognized_items_json=json.dumps({"header": header_meta, "items": recognized_items}, ensure_ascii=False),
        supplier_legal_name=payload.supplier_legal_name or supplier,
        invoice_number=invoice_number,
        invoice_date=payload.invoice_date,
    )
    db.add(document)

    for index, item in enumerate(payload.items, start=1):
        status = ReceivingItemStatus.accepted if _item_is_complete(item) else ReceivingItemStatus.manual_review
        db.add(
            ReceivingItem(
                receiving_id=receiving.id,
                item_name_from_order=None,
                item_name_from_invoice=item.name,
                ordered_quantity=0,
                received_quantity=item.quantity or 0,
                unit=item.unit or "шт",
                ordered_price=0,
                invoice_price=item.price or 0,
                status=status,
                comment=_build_item_comment(item, index),
            )
        )

    db.commit()
    db.refresh(receiving)
    return receiving


def update_invoice_review(db: Session, receiving_id: int, payload) -> Receiving:
    receiving = _get_receiving(db, receiving_id)
    receiving.venue = (
        payload.venue
        or getattr(payload, "trade_point", None)
        or getattr(payload, "iiko_organization", None)
        or receiving.venue
    )
    receiving.supplier = payload.supplier or receiving.supplier
    receiving.delivery_address = payload.delivery_address or receiving.delivery_address

    document = receiving.documents[-1] if receiving.documents else None
    header_meta = _header_payload(payload)
    recognized_items = [_item_payload(item, index) for index, item in enumerate(payload.items, start=1)]
    old_meta = _document_meta(document) if document is not None else {"header": {}, "items": []}
    merged_header = {**old_meta.get("header", {}), **{k: v for k, v in header_meta.items() if v not in (None, "")}}
    # Вариант 3: Google Таблица присылает только бизнес-поля.
    # Старые технические iiko-поля берем из backend metadata и сохраняем до повторного автосопоставления.
    recognized_items = _merge_stored_iiko_metadata(recognized_items, old_meta.get("items") or [])
    mapping_result = auto_fill_iiko_fields(merged_header, recognized_items, supplier_name=payload.supplier or receiving.supplier, venue=payload.venue or receiving.venue)
    merged_header = mapping_result["header"]
    recognized_items = mapping_result["items"]
    if mapping_result.get("notes"):
        merged_header["mapping_notes"] = mapping_result["notes"]
    if document is not None:
        document.invoice_number = payload.invoice_number or document.invoice_number
        document.invoice_date = payload.invoice_date or document.invoice_date
        document.supplier_legal_name = payload.supplier_legal_name or payload.supplier or document.supplier_legal_name
        document.raw_text = payload.raw_text or document.raw_text
        document.recognized_items_json = json.dumps({"header": merged_header, "items": recognized_items}, ensure_ascii=False)
        document.ocr_status = "ocr_processed"
    if payload.invoice_number:
        receiving.order_number = payload.invoice_number

    for item in list(receiving.items):
        db.delete(item)
    db.flush()

    for index, item in enumerate(payload.items, start=1):
        status = ReceivingItemStatus.accepted if _item_is_complete(item) else ReceivingItemStatus.manual_review
        db.add(
            ReceivingItem(
                receiving_id=receiving.id,
                item_name_from_order=None,
                item_name_from_invoice=item.name,
                ordered_quantity=0,
                received_quantity=item.quantity or 0,
                unit=item.unit or "шт",
                ordered_price=0,
                invoice_price=item.price or 0,
                status=status,
                comment=_build_item_comment(item, index),
            )
        )
    receiving.status = ReceivingStatus.ocr_processed
    db.commit()
    db.refresh(receiving)
    return receiving




def get_iiko_reference_status() -> dict:
    context = get_iiko_reference_context(force_refresh=False)
    if context.get("context"):
        refs = context["context"]
        return {
            "status": context.get("status"),
            "cached": context.get("cached", False),
            "counts": {
                "suppliers": len(refs.get("suppliers", [])),
                "products": len(refs.get("products", [])),
                "stores": len(refs.get("stores", [])),
                "units": len(refs.get("units", [])),
                "taxes": len(refs.get("taxes", [])),
            },
        }
    return {"status": context.get("status"), "message": context.get("message")}


def remap_review_with_iiko_references(db: Session, receiving_id: int, force_refresh: bool = False) -> Receiving:
    receiving = _get_receiving(db, receiving_id)
    document = receiving.documents[-1] if receiving.documents else None
    if document is None:
        raise ValueError("Накладная не найдена для автосопоставления")
    if force_refresh:
        invalidate_iiko_reference_cache()
    meta = _document_meta(document)
    header = meta.get("header", {})
    items = meta.get("items", [])
    mapping_result = auto_fill_iiko_fields(header, items, supplier_name=receiving.supplier, venue=receiving.venue)
    header = mapping_result["header"]
    if mapping_result.get("notes"):
        header["mapping_notes"] = mapping_result["notes"]
    document.recognized_items_json = json.dumps({"header": header, "items": mapping_result["items"]}, ensure_ascii=False)
    db.commit()
    db.refresh(receiving)
    return receiving


def build_review_sheet(receiving: Receiving) -> dict:
    """Build the human-facing Google Sheet in the АвтоСнаб invoice-register format."""
    document = receiving.documents[-1] if receiving.documents else None
    meta = _document_meta(document)
    header_meta = meta.get("header", {})
    item_meta = meta.get("items", [])
    items = list(receiving.items)
    calculated_total_sum = _calculate_review_total_sum(items, item_meta)
    total_sum = header_meta.get("total_sum") if header_meta.get("total_sum") not in (None, "") else calculated_total_sum
    issues = validate_review(receiving)

    header_values = _invoice_register_header_values(receiving, document, header_meta, total_sum)
    register_rows = [INVOICE_REGISTER_HEADERS]
    if items:
        for index, item in enumerate(items, start=1):
            row_meta = item_meta[index - 1] if index - 1 < len(item_meta) else {}
            register_rows.append(_invoice_register_item_row(header_values, item, row_meta, index))
    else:
        register_rows.append(_invoice_register_item_row(header_values, None, {}, 1))

    return {
        "review_id": receiving.id,
        "spreadsheet_name": INVOICE_REGISTER_SPREADSHEET_NAME,
        "primary_sheet_name": INVOICE_REGISTER_SHEET_NAME,
        "sheets": {
            INVOICE_REGISTER_SHEET_NAME: register_rows,
        },
        "action": {
            "button_label": "Подтвердить и отправить в iiko",
            "method": "POST",
            "endpoint": f"/api/v1/invoice-review/{receiving.id}/confirm-send",
        },
        "status": "ready" if not issues else "needs_review",
        "issues": issues,
    }


def _invoice_register_header_values(
    receiving: Receiving,
    document: ReceivingDocument | None,
    header_meta: dict[str, Any],
    total_sum: Any,
) -> dict[str, Any]:
    raw_text = document.raw_text if document else ""
    is_torg12_continuation = _looks_like_torg12_continuation_text(raw_text)

    if is_torg12_continuation:
        # На странице-продолжении ТОРГ-12 шапки документа нет.
        # Поэтому не переносим сюда значения из старых метаданных,
        # подписи получателя или случайные OCR-числа вроде 41/12.
        document_number = ""
        document_date = ""
        supplier = ""
        supplier_inn = ""
        consignee = ""
        recipient = ""
        trade_point = ""
        warehouse = ""
        basis = ""
        duplicate_indicator = ""
    else:
        document_number = header_meta.get("document_number") or (document.invoice_number if document else receiving.order_number)
        document_date = document.invoice_date if document else None
        document_date = document_date or header_meta.get("invoice_date") or header_meta.get("incoming_date")
        supplier = _sheet_display_value(receiving.supplier or (document.supplier_legal_name if document else ""))
        consignee = _sheet_display_value(header_meta.get("consignee") or receiving.venue)
        recipient = _sheet_display_value(header_meta.get("recipient") or header_meta.get("buyer") or consignee)
        trade_point = _sheet_display_value(header_meta.get("trade_point") or receiving.venue)
        warehouse = _sheet_display_value(
            header_meta.get("warehouse")
            or header_meta.get("display_store")
            or header_meta.get("iiko_default_store_name")
            or header_meta.get("iiko_default_store_id")
            or ""
        )
        # Заполняем ИНН поставщика только из явно распознанного поля шапки
        # или из самой строки поставщика. Не берём первый ИНН из raw_text:
        # на страницах-продолжениях ТОРГ-12 в тексте часто есть только ИНН
        # получателя/подписанта, и он не должен попадать в колонку поставщика.
        supplier_inn = _sheet_display_value(
            header_meta.get("supplier_inn")
            or _extract_first_inn(supplier)
            or ""
        )
        basis = _sheet_display_value(header_meta.get("basis") or "")
        duplicate_indicator = _sheet_display_value(header_meta.get("duplicate_indicator") or "")

    return {
        "upload_time": _format_datetime_for_sheet((document.created_at if document else None) or receiving.created_at),
        "document_id": 1,
        "duplicate_indicator": duplicate_indicator,
        "document_form": _sheet_display_value(header_meta.get("document_form") or _detect_document_form_from_text(raw_text) or ""),
        "document_date": _sheet_display_value(document_date),
        "document_number": _sheet_display_value(document_number),
        "supplier": supplier,
        "supplier_inn": supplier_inn,
        "consignee": consignee,
        "recipient": recipient,
        "trade_point": trade_point,
        "warehouse": warehouse,
        "basis": basis,
        "total_sum": total_sum,
    }


def _single_value_for_first_item_row(header_values: dict[str, Any], key: str, index: int) -> Any:
    if index == 1:
        return header_values[key]
    return ""


def _invoice_register_item_row(
    header_values: dict[str, Any],
    item: ReceivingItem | None,
    row_meta: dict[str, Any],
    index: int,
) -> list[Any]:
    if item is None:
        item_name = ""
        unit = ""
        quantity = ""
        price = ""
        line_sum = ""
        vat_percent = ""
        vat_sum = ""
        line_sum_with_vat = ""
        unit_in_us = ""
        quantity_in_us = ""
        price_in_us = ""
        ordered_quantity = ""
        price_by_pricelist = ""
        deviation_from_pricelist = ""
        upload_to_us = ""
        status = ""
        manual_reason = ""
        us_product_name = ""
        product_found = ""
    else:
        quantity = item.received_quantity or 0
        price = item.invoice_price or 0
        item_name = item.item_name_from_invoice or item.item_name_from_order or ""
        unit = item.unit
        line_sum = row_meta.get("sum") if row_meta.get("sum") is not None else round(quantity * price, 2)
        vat_percent = _vat_percent_for_sheet(row_meta, item.comment)
        vat_sum = row_meta.get("vat_sum") if row_meta.get("vat_sum") is not None else ""
        line_sum_with_vat = _line_sum_with_vat(line_sum, vat_sum)
        # В пользовательскую таблицу переносим только данные, которые есть
        # в накладной. Поля УС/справочников/ручной корректировки не
        # заполняем автоматически, чтобы в них не попадали случайные OCR-
        # или технические значения.
        unit_in_us = ""
        quantity_in_us = ""
        price_in_us = ""
        ordered_quantity = ""
        price_by_pricelist = ""
        deviation_from_pricelist = ""
        upload_to_us = ""
        status = ""
        manual_reason = ""
        us_product_name = ""
        product_found = ""

    return [
        _single_value_for_first_item_row(header_values, "upload_time", index),
        _single_value_for_first_item_row(header_values, "document_id", index),
        header_values["duplicate_indicator"],
        _single_value_for_first_item_row(header_values, "document_form", index),
        _single_value_for_first_item_row(header_values, "document_date", index),
        _single_value_for_first_item_row(header_values, "document_number", index),
        _single_value_for_first_item_row(header_values, "supplier", index),
        _single_value_for_first_item_row(header_values, "supplier_inn", index),
        _single_value_for_first_item_row(header_values, "consignee", index),
        _single_value_for_first_item_row(header_values, "recipient", index),
        _single_value_for_first_item_row(header_values, "trade_point", index),
        _single_value_for_first_item_row(header_values, "warehouse", index),
        _single_value_for_first_item_row(header_values, "basis", index),
        item_name,
        "",
        us_product_name,
        product_found,
        unit,
        quantity,
        price,
        line_sum,
        vat_percent,
        vat_sum,
        line_sum_with_vat,
        _single_value_for_first_item_row(header_values, "total_sum", index),
        unit_in_us,
        quantity_in_us,
        price_in_us,
        "",
        "",
        ordered_quantity,
        price_by_pricelist,
        "",
        "",
        deviation_from_pricelist,
        upload_to_us,
        status,
        manual_reason,
    ]

def build_review_csv(receiving: Receiving) -> str:
    sheet = build_review_sheet(receiving)
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow([sheet["spreadsheet_name"]])
    writer.writerow([])
    for title, rows in sheet["sheets"].items():
        writer.writerow([title])
        for row in rows:
            writer.writerow(row)
        writer.writerow([])
    return output.getvalue()


def save_review_csv(receiving: Receiving, base_dir: str = "exports") -> str:
    target_dir = Path(base_dir)
    target_dir.mkdir(parents=True, exist_ok=True)
    filename = f"invoice_review_{receiving.id}.csv"
    path = target_dir / filename
    path.write_text(build_review_csv(receiving), encoding="utf-8-sig")
    return str(path)


def build_iiko_preview(receiving: Receiving, target_organization: str | None = None, target_warehouse: str | None = None, target_organization_id: str | None = None, target_warehouse_id: str | None = None) -> dict:
    document = receiving.documents[-1] if receiving.documents else None
    meta = _document_meta(document)
    header_meta = meta.get("header", {})
    item_meta = meta.get("items", [])
    warehouse = target_warehouse_id or target_warehouse or header_meta.get("iiko_default_store_id") or "Основной склад"
    items = []
    for index, item in enumerate(receiving.items, start=1):
        row_meta = item_meta[index - 1] if index - 1 < len(item_meta) else {}
        if item.status not in {ReceivingItemStatus.rejected, ReceivingItemStatus.crossed_out}:
            quantity = item.received_quantity or 0
            price = item.invoice_price or 0
            line_sum = row_meta.get("sum") if row_meta.get("sum") is not None else round(quantity * price, 2)
            items.append(
                {
                    "num": row_meta.get("line_number") or index,
                    "name": item.item_name_from_invoice or item.item_name_from_order or "",
                    "iikoProductId": row_meta.get("iiko_product_id"),
                    "productArticle": row_meta.get("product_article"),
                    "supplierProduct": row_meta.get("supplier_product"),
                    "supplierProductArticle": row_meta.get("supplier_product_article"),
                    "amount": quantity,
                    "quantity": quantity,
                    "unit": item.unit,
                    "amountUnit": row_meta.get("amount_unit") or item.unit,
                    "price": price,
                    "sum": line_sum,
                    "vatPercent": row_meta.get("vat_percent") if row_meta.get("vat_percent") is not None else _extract_vat_percent(item.comment),
                    "vatSum": row_meta.get("vat_sum"),
                    "store": row_meta.get("store_id") or warehouse,
                    "status": item.status.value,
                    "mappingStatus": row_meta.get("mapping_status") or "ready",
                    "mappingError": row_meta.get("mapping_error"),
                    "comment": item.comment,
                }
            )
    total_sum = round(sum(float(item.get("sum") or 0) for item in items), 2)
    preview = {
        "review_id": receiving.id,
        "target_system": "iiko",
        "target": {
            "organization": target_organization or header_meta.get("iiko_organization") or receiving.venue,
            "organizationId": target_organization_id or header_meta.get("iiko_organization_id"),
            "warehouse": warehouse,
            "defaultStoreId": warehouse,
            "venue": receiving.venue,
        },
        "supplier": {
            "displayName": receiving.supplier,
            "legalName": document.supplier_legal_name if document else None,
            "iikoSupplierId": header_meta.get("iiko_supplier_id"),
        },
        "invoice": {
            "number": document.invoice_number if document else receiving.order_number,
            "documentNumber": header_meta.get("document_number") or (document.invoice_number if document else receiving.order_number),
            "date": document.invoice_date if document else None,
            "incomingDate": header_meta.get("incoming_date") or (document.invoice_date if document else None),
            "dueDate": header_meta.get("due_date"),
            "totalSum": total_sum,
            "files": [doc.file_url or doc.file_id for doc in receiving.documents if doc.file_url or doc.file_id],
        },
        "items": items,
        "statusBeforeSend": receiving.status.value,
        "issues": validate_review(receiving),
        "source": "autosnab_iiko_incoming_invoice_adapter",
    }
    preview["iikoXml"] = build_incoming_invoice_xml(preview)
    return preview


def confirm_and_send_to_iiko(db: Session, receiving_id: int, payload) -> AccountingExport:
    receiving = _get_receiving(db, receiving_id)
    if not payload.approved:
        raise ValueError("Перед отправкой пользователь должен подтвердить проверку накладной")
    issues = validate_review(receiving)
    if issues and not payload.allow_with_warnings:
        raise ValueError("Накладная требует проверки: " + "; ".join(issues))

    preview = build_iiko_preview(
        receiving,
        payload.target_organization,
        payload.target_warehouse,
        payload.target_organization_id,
        payload.target_warehouse_id,
    )
    preview["userConfirmation"] = {
        "approved": payload.approved,
        "approvedBy": payload.approved_by,
        "comment": payload.comment,
        "confirmedAt": datetime.utcnow().isoformat(),
    }
    preview["comment"] = payload.comment or preview.get("comment")

    try:
        export_payload = build_iiko_export_payload(preview, dry_run=payload.dry_run)
        iiko_result = export_payload.get("iikoResult", {})
        if payload.dry_run:
            status = "iiko_xml_prepared"
        elif iiko_result.get("status") == "sent_to_iiko":
            status = "sent_to_iiko"
        else:
            status = "iiko_sent_mock"
        error_message = None
    except Exception as exc:  # noqa: BLE001 - external iiko errors must be persisted
        export_payload = {
            "preview": preview,
            "iikoXml": build_incoming_invoice_xml(preview),
            "iikoResult": {"status": "iiko_error", "error": str(exc)},
            "source": "autosnab_iiko_incoming_invoice_adapter",
        }
        status = "iiko_error"
        error_message = str(exc)

    export = AccountingExport(
        receiving_id=receiving.id,
        request_id=receiving.request_id,
        order_number=receiving.order_number,
        target_system="iiko",
        status=status,
        payload_json=json.dumps(export_payload, ensure_ascii=False),
        error_message=error_message,
    )
    if status == "iiko_error":
        receiving.status = ReceivingStatus.accounting_error
    elif payload.dry_run:
        receiving.status = ReceivingStatus.confirmed_full
    else:
        receiving.status = ReceivingStatus.sent_to_accounting
    receiving.comment = payload.comment or receiving.comment
    db.add(export)
    db.commit()
    db.refresh(export)
    return export


def build_apps_script_sample(receiving: Receiving, public_api_base_url: str = "https://YOUR_API_HOST") -> str:
    endpoint = f"{public_api_base_url.rstrip()}/api/v1/invoice-review/{receiving.id}/sync-sheet-and-confirm-send"
    return f"""function onOpen() {{
  SpreadsheetApp.getUi()
    .createMenu('АвтоСнаб')
    .addItem('👁 Предпросмотр отправки', 'previewInvoiceForIiko')
    .addItem('✅ Отправить в iiko', 'sendInvoiceToIiko')
    .addToUi();
}}

function readInvoiceRows_() {{
  const ss = SpreadsheetApp.getActiveSpreadsheet();
  const sheet = ss.getSheetByName('Накладные');
  const values = sheet.getDataRange().getValues();
  if (values.length < 2) return [];
  const headers = values[0].map(cell => String(cell).trim());
  const rows = [];
  for (let i = 1; i < values.length; i++) {{
    const row = {{}};
    headers.forEach((header, index) => {{
      if (header) row[header] = values[i][index];
    }});
    rows.push(row);
  }}
  return rows;
}}

function value_(row, fieldName) {{
  const value = row[fieldName];
  return value === undefined || value === null ? '' : value;
}}

function firstBusinessRow_(rows) {{
  for (const row of rows) {{
    if (value_(row, '№ Документа') || value_(row, 'Поставщик') || value_(row, 'Наименование товара из документа') || value_(row, 'Наименование товара')) {{
      return row;
    }}
  }}
  return {{}};
}}

function uploadDisabled_(value) {{
  const text = String(value || '').trim().toLowerCase();
  return ['нет', 'no', 'false', '0', 'не загружать'].indexOf(text) >= 0;
}}

function readItems_() {{
  const rows = readInvoiceRows_();
  const items = [];
  rows.forEach((row, index) => {{
    const name = String(value_(row, 'Наименование товара из документа') || value_(row, 'Наименование товара') || '').trim();
    if (!name || uploadDisabled_(value_(row, 'Загрузить в УС'))) return;
    const vatPercent = value_(row, 'Ставка НДС %') === '' ? null : Number(value_(row, 'Ставка НДС %'));
    items.push({{
      line_number: index + 1,
      name: name,
      quantity: Number(value_(row, 'Кол-во из документа') || value_(row, 'Количество') || value_(row, 'Кол-во') || 0),
      unit: String(value_(row, 'Ед.изм.') || 'шт'),
      price: Number(value_(row, 'Цена за единицу') || value_(row, 'Цена') || 0),
      sum: value_(row, 'Стоимость без НДС') === '' ? null : Number(value_(row, 'Стоимость без НДС')),
      vat_percent: vatPercent,
      vat: vatPercent === null ? null : String(vatPercent) + '%',
      vat_sum: value_(row, 'Сумма НДС') === '' ? null : Number(value_(row, 'Сумма НДС')),
      comment: String(value_(row, 'Причина ручной корректировки') || '') || null
    }});
  }});
  return items;
}}

function buildPayload_() {{
  const rows = readInvoiceRows_();
  const summary = firstBusinessRow_(rows);
  const venue = String(value_(summary, 'Торговая точка') || value_(summary, 'Грузополучатель') || value_(summary, 'Получатель') || '');
  const warehouse = String(value_(summary, 'Склад') || 'Основной склад');
  const invoiceNumber = String(value_(summary, '№ Документа') || '');
  const invoiceDate = String(value_(summary, 'Дата документа') || '');
  const supplier = String(value_(summary, 'Поставщик') || '');
  return {{
    approved: true,
    dry_run: false,
    allow_with_warnings: false,
    target_organization: venue,
    target_organization_id: null,
    target_warehouse: warehouse,
    target_warehouse_id: null,
    approved_by: Session.getActiveUser().getEmail(),
    comment: 'Подтверждено из Google Таблицы',
    supplier: supplier,
    supplier_legal_name: supplier,
    iiko_supplier_id: null,
    invoice_number: invoiceNumber,
    document_number: invoiceNumber,
    invoice_date: invoiceDate,
    incoming_date: invoiceDate,
    venue: venue,
    delivery_address: String(value_(summary, 'Грузополучатель') || venue),
    display_store: warehouse,
    iiko_default_store_id: warehouse,
    document_form: String(value_(summary, 'Форма документа') || ''),
    supplier_inn: String(value_(summary, 'ИНН Поставщика') || ''),
    consignee: String(value_(summary, 'Грузополучатель') || ''),
    recipient: String(value_(summary, 'Получатель') || ''),
    trade_point: String(value_(summary, 'Торговая точка') || ''),
    warehouse: warehouse,
    basis: String(value_(summary, 'Основание') || ''),
    total_sum: value_(summary, 'Сумма накладной') === '' ? null : Number(value_(summary, 'Сумма накладной')),
    items: readItems_()
  }};
}}

function previewInvoiceForIiko() {{
  const payload = buildPayload_();
  const total = payload.items.reduce((sum, item) => sum + (Number(item.sum) || Number(item.quantity) * Number(item.price)), 0);
  SpreadsheetApp.getUi().alert(
    'Предпросмотр отправки в iiko',
    'Поставщик: ' + payload.supplier + '\n' +
    'Точка: ' + payload.venue + '\n' +
    'Склад: ' + payload.target_warehouse + '\n' +
    'Накладная: ' + payload.invoice_number + ' от ' + payload.invoice_date + '\n' +
    'Позиций: ' + payload.items.length + '\n' +
    'Итого: ' + total.toFixed(2),
    SpreadsheetApp.getUi().ButtonSet.OK
  );
}}

function sendInvoiceToIiko() {{
  const ss = SpreadsheetApp.getActiveSpreadsheet();
  const url = '{endpoint}';
  const payload = buildPayload_();
  const confirm = SpreadsheetApp.getUi().alert(
    'Отправить в iiko?',
    'Backend прочитает лист «Накладные» и отправит накладную ' + payload.invoice_number + '. Отправить?',
    SpreadsheetApp.getUi().ButtonSet.YES_NO
  );
  if (confirm !== SpreadsheetApp.getUi().Button.YES) {{
    return;
  }}
  const response = UrlFetchApp.fetch(url, {{
    method: 'post',
    contentType: 'application/json',
    payload: JSON.stringify(payload),
    muteHttpExceptions: true
  }});
  const statusText = response.getResponseCode() + ': ' + response.getContentText();
  ss.toast(statusText, 'Статус отправки в iiko');
}}
"""

def create_real_google_sheet_for_review(db: Session, receiving: Receiving, public_api_base_url: str | None = None) -> dict:
    sheet = build_review_sheet(receiving)
    result = create_invoice_review_spreadsheet(
        receiving,
        sheet,
        None,
        public_api_base_url=public_api_base_url or "https://YOUR_API_HOST",
    )
    export = AccountingExport(
        receiving_id=receiving.id,
        request_id=receiving.request_id,
        order_number=receiving.order_number,
        target_system="google_sheets",
        status="spreadsheet_created",
        payload_json=serialize_sheet_result(result),
    )
    db.add(export)
    db.commit()
    return result



def get_latest_google_spreadsheet_info(db: Session, receiving_id: int) -> dict[str, Any]:
    export = (
        db.query(AccountingExport)
        .filter(AccountingExport.receiving_id == receiving_id, AccountingExport.target_system == "google_sheets")
        .order_by(AccountingExport.id.desc())
        .first()
    )
    if export is None:
        raise ValueError("Для этой накладной Google Таблица ещё не создана")
    try:
        payload = json.loads(export.payload_json or "{}")
    except json.JSONDecodeError as exc:
        raise ValueError("Не удалось прочитать данные созданной Google Таблицы") from exc
    spreadsheet_id = payload.get("spreadsheet_id")
    if not spreadsheet_id:
        raise ValueError("В истории создания не найден spreadsheet_id")
    return {
        "spreadsheet_id": spreadsheet_id,
        "spreadsheet_url": payload.get("spreadsheet_url"),
        "spreadsheet_name": payload.get("spreadsheet_name"),
    }


def send_google_sheet_and_confirm_to_iiko(
    db: Session,
    receiving_id: int,
    allow_with_warnings: bool = True,
    dry_run: bool = False,
) -> AccountingExport:
    spreadsheet = get_latest_google_spreadsheet_info(db, receiving_id)
    sheet_values = _read_google_sheet_values(spreadsheet["spreadsheet_id"])
    payload = _build_sync_payload_from_sheet(sheet_values, allow_with_warnings=allow_with_warnings, dry_run=dry_run)
    return sync_sheet_and_confirm_to_iiko(db, receiving_id, payload)


def _read_google_sheet_values(spreadsheet_id: str) -> dict[str, list[list[Any]]]:
    try:
        from googleapiclient.discovery import build
    except ImportError as exc:
        raise ValueError("Не установлены зависимости Google API. Выполните pip install -r backend/requirements.txt.") from exc
    from app.services.google_oauth_service import get_google_user_credentials

    credentials = get_google_user_credentials()
    sheets_service = build("sheets", "v4", credentials=credentials)
    spreadsheet = sheets_service.spreadsheets().get(spreadsheetId=spreadsheet_id, fields="sheets.properties.title").execute()
    titles = {sheet["properties"]["title"] for sheet in spreadsheet.get("sheets", [])}
    requested_ranges = []
    range_keys = []
    if INVOICE_REGISTER_SHEET_NAME in titles:
        requested_ranges.append(f"{INVOICE_REGISTER_SHEET_NAME}!A1:AL500")
        range_keys.append("invoices")
    if "Накладная" in titles:
        requested_ranges.append("Накладная!A1:H30")
        range_keys.append("summary")
    if "Товарные позиции" in titles:
        requested_ranges.append("Товарные позиции!A1:J500")
        range_keys.append("items")
    if not requested_ranges:
        return {"invoices": [], "summary": [], "items": []}

    result = sheets_service.spreadsheets().values().batchGet(
        spreadsheetId=spreadsheet_id,
        ranges=requested_ranges,
    ).execute()
    value_ranges = result.get("valueRanges", [])
    sheet_values = {"invoices": [], "summary": [], "items": []}
    for key, value_range in zip(range_keys, value_ranges, strict=False):
        sheet_values[key] = value_range.get("values", [])
    return sheet_values


def _build_sync_payload_from_sheet(sheet_values: dict[str, list[list[Any]]], allow_with_warnings: bool, dry_run: bool):
    from app.schemas.invoice_review import SyncSheetAndConfirmRequest

    invoice_rows = sheet_values.get("invoices") or []
    if invoice_rows:
        summary = _register_summary_dict_from_rows(invoice_rows)
        items = _register_items_from_sheet_rows(invoice_rows)
        invoice_number = summary.get("№ Документа") or ""
        invoice_date = summary.get("Дата документа") or ""
        venue = summary.get("Торговая точка") or summary.get("Грузополучатель") or summary.get("Получатель") or ""
        warehouse = summary.get("Склад") or "Основной склад"
        supplier = summary.get("Поставщик") or ""
        return SyncSheetAndConfirmRequest(
            approved=True,
            dry_run=dry_run,
            allow_with_warnings=allow_with_warnings,
            target_organization=venue,
            target_organization_id=None,
            target_warehouse=warehouse,
            target_warehouse_id=None,
            approved_by="Google Таблица",
            comment="Подтверждено через кнопку Google Таблицы",
            supplier=supplier,
            supplier_legal_name=supplier,
            iiko_supplier_id=None,
            invoice_number=invoice_number,
            document_number=invoice_number,
            invoice_date=invoice_date,
            incoming_date=invoice_date,
            venue=venue,
            delivery_address=summary.get("Грузополучатель") or venue,
            display_store=warehouse,
            iiko_default_store_id=warehouse or None,
            document_form=summary.get("Форма документа") or None,
            supplier_inn=summary.get("ИНН Поставщика") or None,
            consignee=summary.get("Грузополучатель") or None,
            recipient=summary.get("Получатель") or None,
            trade_point=summary.get("Торговая точка") or None,
            warehouse=warehouse or None,
            basis=summary.get("Основание") or None,
            total_sum=_float_from_sheet(summary.get("Сумма накладной")),
            items=items,
        )

    summary = _summary_dict_from_rows(sheet_values.get("summary") or [])
    items = _items_from_sheet_rows(sheet_values.get("items") or [])
    return SyncSheetAndConfirmRequest(
        approved=True,
        dry_run=dry_run,
        allow_with_warnings=allow_with_warnings,
        target_organization=summary.get("Заведение / точка доставки") or "",
        target_organization_id=None,
        target_warehouse=summary.get("Склад / подразделение") or "Основной склад",
        target_warehouse_id=None,
        approved_by="Google Таблица",
        comment=summary.get("Комментарий пользователя") or "Подтверждено через кнопку Google Таблицы",
        supplier=summary.get("Поставщик") or "",
        supplier_legal_name=summary.get("Поставщик") or "",
        iiko_supplier_id=None,
        invoice_number=summary.get("Номер накладной") or "",
        document_number=summary.get("Номер накладной") or "",
        invoice_date=summary.get("Дата накладной") or "",
        incoming_date=summary.get("Дата накладной") or "",
        venue=summary.get("Заведение / точка доставки") or "",
        iiko_default_store_id=summary.get("Склад / подразделение") or None,
        items=items,
    )


def _register_summary_dict_from_rows(rows: list[list[Any]]) -> dict[str, Any]:
    dict_rows = _sheet_dict_rows(rows)
    for row in dict_rows:
        if any(row.get(field) not in (None, "") for field in ("№ Документа", "Поставщик", "Наименование товара из документа", "Наименование товара")):
            return row
    return {}


def _register_items_from_sheet_rows(rows: list[list[Any]]) -> list[dict[str, Any]]:
    items = []
    for index, row in enumerate(_sheet_dict_rows(rows), start=1):
        name = str(row.get("Наименование товара из документа") or row.get("Наименование товара") or "").strip()
        if not name or _is_upload_disabled(row.get("Загрузить в УС")):
            continue
        vat_percent = _float_from_sheet(row.get("Ставка НДС %"))
        vat_sum = _float_from_sheet(row.get("Сумма НДС"))
        line_sum = _float_from_sheet(row.get("Стоимость без НДС"))
        total_with_vat = _float_from_sheet(row.get("Общая стоимость"))
        if line_sum is None and total_with_vat is not None and vat_sum is not None:
            line_sum = round(total_with_vat - vat_sum, 2)
        quantity = _float_from_sheet(row.get("Кол-во из документа"))
        if quantity is None:
            quantity = _float_from_sheet(row.get("Количество"))
        if quantity is None:
            quantity = _float_from_sheet(row.get("Кол-во"))
        price = _float_from_sheet(row.get("Цена за единицу"))
        if price is None:
            price = _float_from_sheet(row.get("Цена"))
        items.append(
            {
                "line_number": index,
                "name": name,
                "quantity": quantity or 0,
                "unit": str(row.get("Ед.изм.") or "шт"),
                "price": price or 0,
                "sum": line_sum,
                "vat_percent": vat_percent,
                "vat": f"{vat_percent:g}%" if vat_percent is not None else None,
                "vat_sum": vat_sum,
                "comment": str(row.get("Причина ручной корректировки") or "") or None,
                "mapping_status": "ready",
                "mapping_error": None,
            }
        )
    return items


def _is_upload_disabled(value: Any) -> bool:
    text = str(value or "").strip().lower()
    return text in {"нет", "no", "false", "0", "не загружать"}


def _sheet_dict_rows(rows: list[list[Any]]) -> list[dict[str, Any]]:
    if not rows:
        return []
    header_index = None
    for index, row in enumerate(rows[:10]):
        normalized = [str(cell).strip() for cell in row]
        if ("Наименование товара из документа" in normalized or "Наименование товара" in normalized) and "№ Документа" in normalized:
            header_index = index
            break
    if header_index is None:
        return []
    headers = [str(cell).strip() for cell in rows[header_index]]
    result = []
    for row in rows[header_index + 1 :]:
        normalized = list(row) + [""] * (len(headers) - len(row))
        result.append({header: normalized[column_index] for column_index, header in enumerate(headers) if header})
    return result


def _summary_dict_from_rows(rows: list[list[Any]]) -> dict[str, Any]:
    result = {}
    known_fields = {
        "Поставщик",
        "Номер накладной",
        "Дата накладной",
        "Заведение / точка доставки",
        "Склад / подразделение",
        "Итоговая сумма",
        "Комментарий пользователя",
    }

    # Новый формат: заголовки по столбцам, данные одной строкой ниже.
    for row_index, row in enumerate(rows):
        header = [str(cell).strip() for cell in row]
        known_count = sum(1 for cell in header if cell in known_fields)
        if known_count >= 2 and row_index + 1 < len(rows):
            values = list(rows[row_index + 1])
            for column_index, field_name in enumerate(header):
                if field_name in known_fields:
                    result[field_name] = values[column_index] if column_index < len(values) else ""
            return result

    # Старый формат: в колонке A название поля, в колонке B значение.
    for row in rows:
        if len(row) >= 2 and str(row[0]).strip():
            field_name = str(row[0]).strip()
            if field_name in {"Поле", "Отправить в iiko"}:
                continue
            result[field_name] = row[1]
    return result


def _items_from_sheet_rows(rows: list[list[Any]]) -> list[dict[str, Any]]:
    items = []
    if not rows:
        return items

    header = [str(cell).strip().lower() for cell in rows[0]]
    old_layout = len(header) > 2 and "статус проверки" in header[1] and "что исправить" in header[2]

    for index, row in enumerate(rows[1:], start=1):
        if old_layout:
            normalized = list(row) + [""] * (11 - len(row))
            name = str(normalized[3] or "").strip()
            if not name:
                continue
            vat_percent = _float_from_sheet(normalized[8])
            item = {
                "line_number": _int_from_sheet(normalized[0]) or index,
                "name": name,
                "quantity": _float_from_sheet(normalized[4]) or 0,
                "unit": str(normalized[5] or "шт"),
                "price": _float_from_sheet(normalized[6]) or 0,
                "sum": _float_from_sheet(normalized[7]),
                "vat_percent": vat_percent,
                "vat": f"{vat_percent:g}%" if vat_percent is not None else None,
                "vat_sum": _float_from_sheet(normalized[9]),
                "comment": str(normalized[10]) if normalized[10] else None,
                "mapping_status": "ready",
                "mapping_error": None,
            }
        else:
            normalized = list(row) + [""] * (10 - len(row))
            name = str(normalized[1] or "").strip()
            if not name:
                continue
            vat_percent = _float_from_sheet(normalized[6])
            vat_sum = _float_from_sheet(normalized[7])
            line_sum = _float_from_sheet(normalized[5])
            sum_with_vat = _float_from_sheet(normalized[8])
            if line_sum is None and sum_with_vat is not None and vat_sum is not None:
                line_sum = round(sum_with_vat - vat_sum, 2)
            item = {
                "line_number": _int_from_sheet(normalized[0]) or index,
                "name": name,
                "quantity": _float_from_sheet(normalized[2]) or 0,
                "unit": str(normalized[3] or "шт"),
                "price": _float_from_sheet(normalized[4]) or 0,
                "sum": line_sum,
                "vat_percent": vat_percent,
                "vat": f"{vat_percent:g}%" if vat_percent is not None else None,
                "vat_sum": vat_sum,
                "comment": str(normalized[9]) if normalized[9] else None,
                "mapping_status": "ready",
                "mapping_error": None,
            }
        items.append(item)
    return items


def _float_from_sheet(value: Any) -> float | None:
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    cleaned = str(value).replace("\xa0", "").replace(" ", "").replace(",", ".").replace("%", "").strip()
    if not cleaned:
        return None
    try:
        return float(cleaned)
    except ValueError:
        return None


def _int_from_sheet(value: Any) -> int | None:
    number = _float_from_sheet(value)
    if number is None:
        return None
    return int(number)


def _write_google_sheet_send_status(spreadsheet_id: str, status: str, error_message: str | None = None) -> None:
    # Статусы отправки больше не записываются в лист «Накладная»,
    # чтобы в пользовательской Google Таблице не появлялись служебные строки.
    return

def sync_sheet_and_confirm_to_iiko(db: Session, receiving_id: int, payload) -> AccountingExport:
    from app.schemas.invoice_review import InvoiceReviewUpdateRequest, RecognizedInvoiceItem

    receiving = _get_receiving(db, receiving_id)
    if payload.items:
        update_payload = InvoiceReviewUpdateRequest(
            raw_text=None,
            supplier=payload.supplier,
            supplier_legal_name=payload.supplier_legal_name,
            iiko_supplier_id=payload.iiko_supplier_id,
            invoice_date=payload.invoice_date,
            invoice_number=payload.invoice_number,
            document_number=payload.document_number,
            incoming_date=payload.incoming_date,
            due_date=payload.due_date,
            venue=payload.venue or payload.trade_point or payload.target_organization,
            delivery_address=payload.delivery_address,
            display_store=payload.display_store or payload.warehouse or payload.target_warehouse,
            iiko_default_store_id=payload.iiko_default_store_id or payload.target_warehouse_id or payload.target_warehouse,
            iiko_organization=payload.iiko_organization or payload.target_organization,
            iiko_organization_id=payload.iiko_organization_id or payload.target_organization_id,
            document_form=payload.document_form,
            supplier_inn=payload.supplier_inn,
            consignee=payload.consignee,
            recipient=payload.recipient,
            trade_point=payload.trade_point,
            warehouse=payload.warehouse,
            basis=payload.basis,
            total_sum=payload.total_sum,
            items=[RecognizedInvoiceItem(**item.model_dump()) for item in payload.items],
        )
        receiving = update_invoice_review(db, receiving_id, update_payload)
    return confirm_and_send_to_iiko(db, receiving.id, payload)


def validate_review(receiving: Receiving) -> list[str]:
    document = receiving.documents[-1] if receiving.documents else None
    meta = _document_meta(document)
    header = meta.get("header", {})
    item_meta = meta.get("items", [])
    issues = []
    if not receiving.supplier or receiving.supplier == "Поставщик не распознан":
        issues.append("не распознан поставщик")
    if not receiving.venue or receiving.venue == "Точка доставки не распознана":
        issues.append("не распознана точка доставки / организация")
    if document is None:
        issues.append("не загружена накладная")
    else:
        if not document.invoice_number:
            issues.append("не распознан номер накладной")
        if not document.invoice_date:
            issues.append("не распознана дата накладной")
    if header.get("iiko_mapping_status") == "needs_review":
        issues.append("требуется проверка сопоставления шапки накладной: " + (header.get("iiko_mapping_error") or "нет уверенного совпадения"))
    if not header.get("iiko_supplier_id"):
        issues.append("не указан поставщик iiko/supplier id")
    if not (header.get("iiko_default_store_id") or header.get("target_warehouse")):
        issues.append("не указан склад iiko/defaultStore")
    if not receiving.items:
        issues.append("нет товарных позиций")
    for index, item in enumerate(receiving.items, start=1):
        row_meta = item_meta[index - 1] if index - 1 < len(item_meta) else {}
        item_name = item.item_name_from_invoice or item.item_name_from_order or f"строка {index}"
        if not item_name:
            issues.append("есть позиция без наименования")
        if row_meta.get("mapping_status") == "needs_review":
            issues.append(f"требуется проверка сопоставления по позиции: {item_name} ({row_meta.get('mapping_error') or 'нет уверенного совпадения'})")
        if not (row_meta.get("iiko_product_id") or row_meta.get("product_article")):
            issues.append(f"нет iiko product/productArticle по позиции: {item_name}")
        if not (row_meta.get("line_number") or index):
            issues.append(f"нет num по позиции: {item_name}")
        if (item.received_quantity or 0) <= 0:
            issues.append(f"некорректное количество по позиции: {item_name}")
        if (item.invoice_price or 0) < 0:
            issues.append(f"некорректная цена по позиции: {item_name}")
        line_sum = row_meta.get("sum")
        if line_sum is None:
            line_sum = round((item.received_quantity or 0) * (item.invoice_price or 0), 2)
        if line_sum is None or float(line_sum) < 0:
            issues.append(f"нет или некорректная sum по позиции: {item_name}")
    return list(dict.fromkeys(issues))


def _get_receiving(db: Session, receiving_id: int) -> Receiving:
    receiving = db.get(Receiving, receiving_id)
    if receiving is None:
        raise ValueError("Проверка накладной не найдена")
    return receiving


def _price_deviation(price: Any, price_by_pricelist: Any) -> float | str:
    price_decimal = _decimal_or_none(price)
    pricelist_decimal = _decimal_or_none(price_by_pricelist)
    if price_decimal is None or pricelist_decimal is None:
        return ""
    return float((price_decimal - pricelist_decimal).quantize(Decimal("0.01")))


def _invoice_register_status(item: ReceivingItem, row_meta: dict[str, Any]) -> str:
    if row_meta.get("mapping_status") == "needs_review" or item.status == ReceivingItemStatus.manual_review:
        return "Требует проверки"
    if item.status == ReceivingItemStatus.rejected:
        return "Отклонено"
    if item.status == ReceivingItemStatus.crossed_out:
        return "Вычеркнуто"
    return "Готово"


def _line_sum_with_vat(line_sum: Any, vat_sum: Any) -> float | str:
    line_decimal = _decimal_or_none(line_sum)
    vat_decimal = _decimal_or_none(vat_sum)
    if line_decimal is None:
        return ""
    if vat_decimal is None:
        return float(line_decimal.quantize(Decimal("0.01")))
    return float((line_decimal + vat_decimal).quantize(Decimal("0.01")))


def _calculate_review_total_sum(items: list[ReceivingItem], item_meta: list[dict[str, Any]]) -> float:
    total = Decimal("0")
    for index, item in enumerate(items, start=1):
        row_meta = item_meta[index - 1] if index - 1 < len(item_meta) else {}
        line_sum = _decimal_or_none(row_meta.get("sum"))
        if line_sum is None:
            line_sum = Decimal(str(item.received_quantity or 0)) * Decimal(str(item.invoice_price or 0))
        vat_sum = _decimal_or_none(row_meta.get("vat_sum"))
        if vat_sum is not None:
            line_sum += vat_sum
        total += line_sum
    return float(total.quantize(Decimal("0.01"))) if total else 0


def _decimal_or_none(value: Any) -> Decimal | None:
    if value in (None, ""):
        return None
    try:
        return Decimal(str(value).replace(" ", "").replace(",", "."))
    except (InvalidOperation, ValueError):
        return None


_SERVICE_PLACEHOLDER_VALUES = {
    "поставщик не распознан",
    "точка доставки не распознана",
    "заведение не распознано",
    "склад не распознан",
    "подразделение не распознано",
    "не распознано",
    "не распознана",
    "требуется проверка",
}


def _format_datetime_for_sheet(value: Any) -> str:
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d %H:%M:%S")
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _extract_first_inn(value: Any) -> str | None:
    if not value:
        return None
    match = re.search(r"\bИНН\s*[:№]?\s*(\d{10,12})\b", str(value), flags=re.IGNORECASE)
    return match.group(1) if match else None


def _detect_document_form_from_text(value: str | None) -> str | None:
    if not value:
        return None
    lowered = value.lower()
    if "универсаль" in lowered and "передаточ" in lowered:
        return "УПД"
    if "торг-12" in lowered or "товарная накладная" in lowered:
        return "ТОРГ-12"
    if "счет-фактура" in lowered or "счёт-фактура" in lowered:
        return "Счет-фактура"
    if "накладная" in lowered:
        return "Накладная"
    return None


def _looks_like_torg12_continuation_text(value: str | None) -> bool:
    if not value:
        return False
    normalized = re.sub(r"\s+", " ", value.lower()).strip()
    has_continuation_marker = (
        bool(re.search(r"страница\s*2\b", normalized))
        or "товарная накладная имеет приложение" in normalized
        or "порядковых номеров записей" in normalized
        or "всего отпущено на сумму" in normalized
    )
    has_full_header = (
        "номер документа" in normalized
        and "дата составления" in normalized
        and any(label in normalized for label in ("поставщик", "грузополучатель", "плательщик"))
    )
    return has_continuation_marker and not has_full_header


def _sheet_display_value(value: Any) -> Any:
    if value in (None, ""):
        return ""
    if not isinstance(value, str):
        return value
    cleaned = re.sub(r"\s+", " ", value).strip()
    cleaned = _remove_torg12_service_hints(cleaned)
    if _is_service_placeholder(cleaned):
        return ""
    return cleaned


def _is_service_placeholder(value: str) -> bool:
    normalized = re.sub(r"\s+", " ", value).strip().lower()
    if not normalized:
        return True
    if normalized in _SERVICE_PLACEHOLDER_VALUES:
        return True
    if normalized.startswith("распознано google drive ocr"):
        return True
    if normalized.endswith("не распознана") or normalized.endswith("не распознано"):
        return True
    return False


def _remove_torg12_service_hints(value: str) -> str:
    service_words = (
        "организац",
        "структурн",
        "адрес",
        "телефон",
        "факс",
        "банковск",
        "реквизит",
        "договор",
        "заказ",
        "должност",
        "расшифровка",
        "подпись",
        "пропись",
    )
    pattern = r"\s*\([^)]*(?:" + "|".join(service_words) + r")[^)]*\)"
    cleaned = re.sub(pattern, "", value, flags=re.IGNORECASE)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" ;,.-—")
    return cleaned


def _clean(value: str | None) -> str | None:
    return value.strip() if value and value.strip() else None


def _header_payload(payload) -> dict[str, Any]:
    display_store = getattr(payload, "display_store", None)
    warehouse = getattr(payload, "warehouse", None)
    iiko_default_store_id = getattr(payload, "iiko_default_store_id", None)
    venue = getattr(payload, "venue", None)
    trade_point = getattr(payload, "trade_point", None) or venue
    return {
        "iiko_supplier_id": getattr(payload, "iiko_supplier_id", None),
        "document_number": getattr(payload, "document_number", None),
        "incoming_date": getattr(payload, "incoming_date", None),
        "due_date": getattr(payload, "due_date", None),
        "document_form": getattr(payload, "document_form", None),
        "supplier_inn": getattr(payload, "supplier_inn", None),
        "consignee": getattr(payload, "consignee", None),
        "recipient": getattr(payload, "recipient", None),
        "trade_point": trade_point,
        "warehouse": warehouse or display_store or iiko_default_store_id,
        "basis": getattr(payload, "basis", None),
        "display_store": display_store or warehouse or iiko_default_store_id,
        "total_sum": getattr(payload, "total_sum", None),
        "iiko_default_store_id": iiko_default_store_id,
        "iiko_organization": getattr(payload, "iiko_organization", None),
        "iiko_organization_id": getattr(payload, "iiko_organization_id", None),
    }

def _item_payload(item, index: int | None = None) -> dict:
    quantity = item.quantity or 0
    price = item.price or 0
    calculated_sum = round(quantity * price, 2)
    return {
        "line_number": item.line_number or index,
        "name": item.name,
        "iiko_product_id": item.iiko_product_id,
        "product_article": item.product_article,
        "supplier_product": item.supplier_product,
        "supplier_product_article": item.supplier_product_article,
        "quantity": quantity,
        "unit": item.unit,
        "amount_unit": item.amount_unit or item.unit,
        "price": price,
        "sum": item.sum if item.sum is not None else calculated_sum,
        "vat": item.vat,
        "vat_percent": item.vat_percent if item.vat_percent is not None else _parse_vat_percent(item.vat),
        "vat_sum": item.vat_sum,
        "store_id": item.store_id,
        "mapping_status": getattr(item, "mapping_status", None),
        "mapping_error": getattr(item, "mapping_error", None),
        "comment": item.comment,
        "confidence": item.confidence,
    }



def _merge_stored_iiko_metadata(new_items: list[dict[str, Any]], old_items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    technical_keys = {
        "iiko_product_id",
        "product_article",
        "supplier_product",
        "supplier_product_article",
        "amount_unit",
        "store_id",
        "mapping_status",
        "mapping_error",
        "iiko_product_name",
        "iiko_product_match_confidence",
        "iiko_unit_name",
        "iiko_unit_match_confidence",
    }
    old_by_line = {str(item.get("line_number") or index): item for index, item in enumerate(old_items, start=1)}
    merged = []
    for index, item in enumerate(new_items, start=1):
        line = str(item.get("line_number") or index)
        old = old_by_line.get(line, {})
        result = dict(item)
        for key in technical_keys:
            if result.get(key) in (None, "") and old.get(key) not in (None, ""):
                result[key] = old.get(key)
        if result.get("vat_percent") is None and old.get("vat_percent") is not None:
            result["vat_percent"] = old.get("vat_percent")
        if result.get("vat_sum") is None and old.get("vat_sum") is not None:
            result["vat_sum"] = old.get("vat_sum")
        merged.append(result)
    return merged

def _document_meta(document: ReceivingDocument | None) -> dict[str, Any]:
    if document is None or not document.recognized_items_json:
        return {"header": {}, "items": []}
    try:
        data = json.loads(document.recognized_items_json)
        if isinstance(data, list):
            return {"header": {}, "items": data}
        if isinstance(data, dict):
            return {"header": data.get("header") or {}, "items": data.get("items") or []}
    except json.JSONDecodeError:
        return {"header": {}, "items": []}
    return {"header": {}, "items": []}


def _item_is_complete(item) -> bool:
    return bool(item.name) and (item.quantity or 0) > 0 and (item.price or 0) >= 0


def _build_item_comment(item, index: int | None = None) -> str | None:
    parts = []
    if item.vat is not None:
        parts.append(f"НДС: {item.vat}")
    if item.vat_percent is not None:
        parts.append(f"vatPercent: {item.vat_percent}")
    if item.vat_sum is not None:
        parts.append(f"vatSum: {item.vat_sum}")
    if item.iiko_product_id:
        parts.append(f"iiko_product_id: {item.iiko_product_id}")
    if item.product_article:
        parts.append(f"productArticle: {item.product_article}")
    if item.confidence is not None:
        parts.append(f"confidence: {item.confidence}")
    if item.comment:
        parts.append(item.comment)
    if item.sum is not None:
        expected = round((item.quantity or 0) * (item.price or 0), 2)
        try:
            incoming_sum = float(Decimal(str(item.sum)))
            if abs(incoming_sum - expected) > 0.01:
                parts.append(f"Расхождение суммы: распознано {incoming_sum}, рассчитано {expected}")
        except (InvalidOperation, ValueError):
            parts.append("Сумма распознана некорректно")
    return "; ".join(parts) if parts else None



def _user_comment_from_item_comment(comment: str | None) -> str:
    """Return only the human comment, hiding technical markers saved in comments."""
    if not comment:
        return ""
    hidden_prefixes = ("НДС:", "vatPercent:", "vatSum:", "iiko_product_id:", "productArticle:", "confidence:")
    parts = []
    for part in comment.split(";"):
        text = part.strip()
        if not text:
            continue
        if text.startswith(hidden_prefixes):
            continue
        parts.append(text)
    return "; ".join(parts)

def _vat_percent_for_sheet(row_meta: dict[str, Any], comment: str | None) -> float | str:
    vat_text = row_meta.get("vat")
    if isinstance(vat_text, str) and vat_text.strip():
        return vat_text.strip()
    vat_percent = row_meta.get("vat_percent")
    if vat_percent is not None:
        try:
            number = float(vat_percent)
            return f"{number:g}%"
        except (TypeError, ValueError):
            return str(vat_percent)
    return _extract_vat_percent(comment) or ""


def _extract_vat_percent(comment: str | None) -> float | str | None:
    if not comment:
        return ""
    for part in comment.split(";"):
        if "vatPercent" in part:
            return part.split(":", 1)[1].strip()
        if "НДС" in part:
            return _parse_vat_percent(part.replace("НДС:", "").strip())
    return ""


def _parse_vat_percent(value: str | None) -> float | None:
    if not value:
        return None
    text = str(value).replace("%", "").replace(",", ".").strip()
    try:
        return float(text)
    except ValueError:
        return None
