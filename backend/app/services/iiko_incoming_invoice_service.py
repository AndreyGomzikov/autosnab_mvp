"""iiko incoming invoice XML builder and optional REST adapter.

Based on iiko Server API documentation:
POST /resto/api/documents/import/incomingInvoice
Content-Type: application/xml
"""

from __future__ import annotations

import html
from datetime import datetime
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

from app.config import settings


def build_incoming_invoice_xml(preview: dict) -> str:
    """Build incomingInvoiceDto XML from MVP-4 preview payload."""
    invoice = preview.get("invoice", {})
    supplier = preview.get("supplier", {})
    target = preview.get("target", {})
    items = preview.get("items", [])

    document_number = invoice.get("documentNumber") or invoice.get("number") or preview.get("review_id")
    date_incoming = _format_iiko_date(invoice.get("date"), prefer="dd.MM.yyyy")
    incoming_date = _format_iiko_date(invoice.get("incomingDate") or invoice.get("date"), prefer="yyyy-MM-dd")
    supplier_value = supplier.get("iikoSupplierId") or supplier.get("legalName") or supplier.get("displayName") or ""
    default_store = target.get("defaultStoreId") or target.get("warehouse") or ""

    lines = ["<?xml version=\"1.0\" encoding=\"UTF-8\" standalone=\"yes\"?>", "<document>"]
    lines.append("  <items>")
    for index, item in enumerate(items, start=1):
        lines.extend(_build_item_xml(item, index, default_store))
    lines.append("  </items>")
    _append_xml_text(lines, "comment", preview.get("comment") or "АвтоСнаб MVP-4: отправлено после проверки в Google Таблице", indent="  ")
    _append_xml_text(lines, "documentNumber", document_number, indent="  ")
    _append_xml_text(lines, "dateIncoming", date_incoming, indent="  ")
    _append_xml_text(lines, "invoice", invoice.get("number"), indent="  ")
    _append_xml_text(lines, "defaultStore", default_store, indent="  ")
    _append_xml_text(lines, "supplier", supplier_value, indent="  ")
    _append_xml_text(lines, "dueDate", _format_iiko_date(invoice.get("dueDate"), prefer="dd.MM.yyyy"), indent="  ")
    _append_xml_text(lines, "incomingDate", incoming_date, indent="  ")
    lines.append("</document>")
    return "\n".join(lines)


def send_incoming_invoice_xml(xml_payload: str, dry_run: bool = False) -> dict:
    """Send XML to iiko when IIKO_INTEGRATION_ENABLED=true.

    If disabled or dry_run, returns a prepared result without external call.
    """
    if dry_run or not settings.iiko_integration_enabled:
        return {
            "status": "iiko_xml_prepared",
            "dry_run": dry_run,
            "integration_enabled": settings.iiko_integration_enabled,
            "xml_length": len(xml_payload),
        }
    if not settings.iiko_base_url:
        raise IikoConfigurationError("Не указан IIKO_BASE_URL для отправки приходной накладной в iiko")

    token = settings.iiko_token or _authorize()
    url = f"{settings.iiko_base_url.rstrip('/')}/resto/api/documents/import/incomingInvoice?{urlencode({'key': token})}"
    request = Request(url, data=xml_payload.encode("utf-8"), method="POST")
    request.add_header("Content-Type", "application/xml; charset=utf-8")
    try:
        with urlopen(request, timeout=settings.iiko_timeout_seconds) as response:  # noqa: S310 - configured host
            body = response.read().decode("utf-8", errors="replace")
            return {
                "status": "sent_to_iiko",
                "http_status": response.status,
                "response_body": body,
            }
    except HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise IikoRequestError(f"iiko HTTP {exc.code}: {body}") from exc
    except URLError as exc:
        raise IikoRequestError(f"Ошибка соединения с iiko: {exc.reason}") from exc


def build_iiko_export_payload(preview: dict, dry_run: bool = False) -> dict:
    xml_payload = build_incoming_invoice_xml(preview)
    result = send_incoming_invoice_xml(xml_payload, dry_run=dry_run)
    return {
        "preview": preview,
        "iikoXml": xml_payload,
        "iikoResult": result,
        "source": "autosnab_iiko_incoming_invoice_adapter",
    }


def _authorize() -> str:
    if not settings.iiko_login or not settings.iiko_password_sha1:
        raise IikoConfigurationError("Для авторизации iiko нужны IIKO_LOGIN и IIKO_PASSWORD_SHA1 или готовый IIKO_TOKEN")
    url = f"{settings.iiko_base_url.rstrip('/')}/resto/api/auth?{urlencode({'login': settings.iiko_login, 'pass': settings.iiko_password_sha1})}"
    try:
        with urlopen(url, timeout=settings.iiko_timeout_seconds) as response:  # noqa: S310 - configured host
            return response.read().decode("utf-8", errors="replace").strip()
    except HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise IikoRequestError(f"iiko auth HTTP {exc.code}: {body}") from exc
    except URLError as exc:
        raise IikoRequestError(f"Ошибка авторизации iiko: {exc.reason}") from exc


def _build_item_xml(item: dict, index: int, default_store: str) -> list[str]:
    num = item.get("num") or item.get("lineNumber") or index
    product = item.get("iikoProductId") or item.get("product")
    product_article = item.get("productArticle")
    amount_unit = item.get("amountUnit") or item.get("unit") or "шт"
    quantity = item.get("amount") if item.get("amount") is not None else item.get("quantity")
    price = item.get("price")
    total = item.get("sum")
    if total is None and quantity is not None and price is not None:
        total = round(float(quantity) * float(price), 2)
    lines = ["    <item>"]
    _append_xml_text(lines, "num", num, indent="      ")
    _append_xml_text(lines, "product", product, indent="      ")
    _append_xml_text(lines, "productArticle", product_article, indent="      ")
    _append_xml_text(lines, "supplierProduct", item.get("supplierProduct"), indent="      ")
    _append_xml_text(lines, "supplierProductArticle", item.get("supplierProductArticle"), indent="      ")
    _append_xml_text(lines, "store", item.get("store") or default_store, indent="      ")
    _append_xml_text(lines, "amount", quantity, indent="      ")
    _append_xml_text(lines, "amountUnit", amount_unit, indent="      ")
    _append_xml_text(lines, "price", price, indent="      ")
    _append_xml_text(lines, "sum", total, indent="      ")
    _append_xml_text(lines, "vatPercent", item.get("vatPercent"), indent="      ")
    _append_xml_text(lines, "vatSum", item.get("vatSum"), indent="      ")
    lines.append("    </item>")
    return lines


def _append_xml_text(lines: list[str], tag: str, value, indent: str = "") -> None:
    if value is None or value == "":
        return
    lines.append(f"{indent}<{tag}>{html.escape(str(value), quote=False)}</{tag}>")


def _format_iiko_date(value: str | None, prefer: str) -> str | None:
    if not value:
        return None
    value = str(value).strip()
    known_formats = ["%Y-%m-%d", "%d.%m.%Y", "%Y-%m-%dT%H:%M:%S"]
    for fmt in known_formats:
        try:
            date_value = datetime.strptime(value[:19], fmt)
            if prefer == "dd.MM.yyyy":
                return date_value.strftime("%d.%m.%Y")
            return date_value.strftime("%Y-%m-%d")
        except ValueError:
            pass
    return value


class IikoConfigurationError(RuntimeError):
    pass


class IikoRequestError(RuntimeError):
    pass
