import csv
import json
from pathlib import Path

from sqlalchemy.orm import Session

from app.models.receiving import Receiving, ReceivingDocument, ReceivingItem

EXPORT_DIR = Path(__file__).resolve().parents[2] / "exports"


def export_receiving_csv(db: Session) -> dict:
    EXPORT_DIR.mkdir(parents=True, exist_ok=True)
    receiving_path = EXPORT_DIR / "priemka.csv"
    items_path = EXPORT_DIR / "priemka_pozicii.csv"
    documents_path = EXPORT_DIR / "priemka_dokumenty.csv"

    receivings = db.query(Receiving).order_by(Receiving.id).all()
    items = db.query(ReceivingItem).order_by(ReceivingItem.id).all()
    documents = db.query(ReceivingDocument).order_by(ReceivingDocument.id).all()

    with receiving_path.open("w", newline="", encoding="utf-8-sig") as file:
        writer = csv.writer(file)
        writer.writerow([
            "Дата создания", "Дата изменения", "ID приемки", "ID заявки", "Номер заявки",
            "Заведение", "Поставщик", "Chat ID", "User ID", "Статус приемки",
            "Кол-во товаров в заявке", "Кол-во совпавших", "Кол-во не найденных", "Кол-во лишних",
            "Комментарий", "Ссылки на накладные",
        ])
        for receiving in receivings:
            links = [doc.file_url or doc.file_id or "" for doc in receiving.documents]
            writer.writerow([
                receiving.created_at.isoformat(), receiving.updated_at.isoformat(), receiving.id,
                receiving.request_id, receiving.order_number, receiving.venue, receiving.supplier,
                receiving.chat_id, receiving.user_id, receiving.status.value,
                len(receiving.order_items), _count_status(receiving.items, "matched"),
                _count_status(receiving.items, "missing"), _count_status(receiving.items, "extra"),
                receiving.comment, json.dumps(links, ensure_ascii=False),
            ])

    with items_path.open("w", newline="", encoding="utf-8-sig") as file:
        writer = csv.writer(file)
        writer.writerow([
            "ID приемки", "ID заявки", "Товар из заявки", "Товар из накладной", "Заказано",
            "Принято", "Ед. изм.", "Цена в заявке", "Цена в накладной", "Статус позиции", "Комментарий",
        ])
        for item in items:
            writer.writerow([
                item.receiving_id, item.receiving.request_id if item.receiving else "",
                item.item_name_from_order, item.item_name_from_invoice, item.ordered_quantity,
                item.received_quantity, item.unit, item.ordered_price, item.invoice_price,
                item.status.value, item.comment,
            ])

    with documents_path.open("w", newline="", encoding="utf-8-sig") as file:
        writer = csv.writer(file)
        writer.writerow([
            "ID приемки", "ID заявки", "Тип файла", "File ID MAX", "Ссылка на файл", "Статус OCR",
            "Юр. название поставщика", "Номер накладной", "Дата накладной", "Raw text OCR",
        ])
        for document in documents:
            writer.writerow([
                document.receiving_id, document.receiving.request_id if document.receiving else "",
                document.file_type, document.file_id, document.file_url, document.ocr_status,
                document.supplier_legal_name, document.invoice_number, document.invoice_date,
                document.raw_text,
            ])

    return {
        "receiving_csv": str(receiving_path),
        "items_csv": str(items_path),
        "documents_csv": str(documents_path),
    }


def _count_status(items: list[ReceivingItem], status: str) -> int:
    return sum(1 for item in items if item.status.value == status)
