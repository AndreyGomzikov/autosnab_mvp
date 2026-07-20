import html
import json
from collections import Counter, defaultdict
from datetime import datetime

from sqlalchemy.orm import Session

from app.models.accounting import AccountingExport
from app.models.receiving import Receiving, ReceivingDocument, ReceivingItemStatus, ReceivingStatus
from app.services.receiving_service import build_accounting_payload

PROBLEM_STATUSES = {
    ReceivingItemStatus.missing,
    ReceivingItemStatus.extra,
    ReceivingItemStatus.quantity_mismatch,
    ReceivingItemStatus.price_mismatch,
    ReceivingItemStatus.replacement_candidate,
    ReceivingItemStatus.crossed_out,
    ReceivingItemStatus.rejected,
    ReceivingItemStatus.manual_review,
}


def build_iiko_payload(receiving: Receiving) -> dict:
    base_payload = build_accounting_payload(receiving)
    return {
        "externalNumber": base_payload["orderNumber"],
        "requestId": base_payload["requestId"],
        "organization": {"name": base_payload["venue"]},
        "supplier": base_payload["supplier"],
        "invoice": base_payload["invoice"],
        "items": [
            {
                "name": item["name"],
                "amount": item["receivedQuantity"],
                "unit": item["unit"],
                "price": item["invoicePrice"],
                "sum": round(item["receivedQuantity"] * item["invoicePrice"], 2),
                "status": item["status"],
                "comment": item["comment"],
            }
            for item in base_payload["items"]
        ],
        "comment": base_payload.get("comment"),
        "source": "autosnab_iiko_adapter",
    }


def create_iiko_export(db: Session, receiving: Receiving, dry_run: bool, comment: str | None = None) -> AccountingExport:
    if receiving.status not in {ReceivingStatus.confirmed_full, ReceivingStatus.confirmed_partial}:
        raise ValueError("Передача в iiko возможна только после подтверждения приемки")

    payload = build_iiko_payload(receiving)
    status = "iiko_prepared" if dry_run else "iiko_sent_mock"
    export = AccountingExport(
        receiving_id=receiving.id,
        request_id=receiving.request_id,
        order_number=receiving.order_number,
        target_system="iiko",
        status=status,
        payload_json=json.dumps(payload, ensure_ascii=False),
    )
    if not dry_run:
        receiving.status = ReceivingStatus.sent_to_accounting
    if comment:
        receiving.comment = comment
    db.add(export)
    db.commit()
    db.refresh(export)
    return export


def document_to_dict(document: ReceivingDocument) -> dict:
    return {
        "id": document.id,
        "receiving_id": document.receiving_id,
        "file_id": document.file_id,
        "file_type": document.file_type,
        "source": document.source,
        "file_url": document.file_url,
        "ocr_status": document.ocr_status,
        "supplier_legal_name": document.supplier_legal_name,
        "invoice_number": document.invoice_number,
        "invoice_date": document.invoice_date,
        "raw_text": document.raw_text,
        "created_at": document.created_at.isoformat() if document.created_at else None,
    }


def build_invoice_html(document: ReceivingDocument) -> str:
    receiving = document.receiving
    title = f"Накладная {document.invoice_number or document.id}"
    raw_text = html.escape(document.raw_text or "Нет распознанного текста")
    file_link = ""
    if document.file_url:
        safe_url = html.escape(document.file_url, quote=True)
        file_link = f'<p><a href="{safe_url}">Открыть файл накладной</a></p>'
    return f"""
    <!doctype html>
    <html lang="ru">
    <head>
        <meta charset="utf-8">
        <title>{html.escape(title)}</title>
        <style>
            body {{ font-family: Arial, sans-serif; margin: 32px; color: #1f2937; }}
            .card {{ border: 1px solid #d1d5db; border-radius: 12px; padding: 20px; max-width: 960px; }}
            .meta {{ color: #4b5563; line-height: 1.6; }}
            pre {{ white-space: pre-wrap; background: #f9fafb; padding: 16px; border-radius: 8px; }}
        </style>
    </head>
    <body>
        <div class="card">
            <h1>{html.escape(title)}</h1>
            <div class="meta">
                <div><b>Заявка:</b> {html.escape(receiving.order_number)}</div>
                <div><b>Заведение:</b> {html.escape(receiving.venue)}</div>
                <div><b>Поставщик в заявке:</b> {html.escape(receiving.supplier)}</div>
                <div><b>Поставщик в накладной:</b> {html.escape(document.supplier_legal_name or "—")}</div>
                <div><b>Дата накладной:</b> {html.escape(document.invoice_date or "—")}</div>
                <div><b>OCR статус:</b> {html.escape(document.ocr_status)}</div>
            </div>
            {file_link}
            <h2>Распознанный текст</h2>
            <pre>{raw_text}</pre>
        </div>
    </body>
    </html>
    """


def build_discrepancy_analytics(db: Session) -> dict:
    receivings = db.query(Receiving).all()
    totals = Counter()
    supplier_stats: dict[str, Counter] = defaultdict(Counter)

    for receiving in receivings:
        totals["receivings"] += 1
        supplier_stats[receiving.supplier]["receivings"] += 1
        if receiving.status == ReceivingStatus.control_required:
            totals["control_required"] += 1
            supplier_stats[receiving.supplier]["control_required"] += 1
        if receiving.comment and "Поставщик в накладной отличается" in receiving.comment:
            totals["supplier_mismatch"] += 1
            supplier_stats[receiving.supplier]["supplier_mismatch"] += 1
        for item in receiving.items:
            status = item.status
            totals[status.value] += 1
            supplier_stats[receiving.supplier][status.value] += 1
            if status in PROBLEM_STATUSES:
                totals["problem_items"] += 1
                supplier_stats[receiving.supplier]["problem_items"] += 1

    by_supplier = []
    for supplier, stats in supplier_stats.items():
        receivings_count = stats["receivings"] or 1
        risk_score = stats["problem_items"] + 2 * stats["supplier_mismatch"] + 2 * stats["control_required"]
        if risk_score >= 5 or stats["supplier_mismatch"] > 0:
            control_status = "control_required"
        elif risk_score >= 2:
            control_status = "watch"
        else:
            control_status = "ok"
        by_supplier.append(
            {
                "supplier": supplier,
                "receivings": stats["receivings"],
                "problem_items": stats["problem_items"],
                "problem_rate": round(stats["problem_items"] / receivings_count, 2),
                "missing": stats["missing"],
                "extra": stats["extra"],
                "quantity_mismatch": stats["quantity_mismatch"],
                "price_mismatch": stats["price_mismatch"],
                "supplier_mismatch": stats["supplier_mismatch"],
                "control_required": stats["control_required"],
                "risk_score": risk_score,
                "control_status": control_status,
            }
        )

    by_supplier.sort(key=lambda item: item["risk_score"], reverse=True)
    return {
        "generated_at": datetime.utcnow().isoformat(),
        "totals": dict(totals),
        "by_supplier": by_supplier,
    }
