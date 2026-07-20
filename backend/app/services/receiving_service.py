from sqlalchemy.orm import Session

from app.models.receiving import (
    OrderItemSnapshot,
    Receiving,
    ReceivingDocument,
    ReceivingItem,
    ReceivingItemStatus,
    ReceivingStatus,
)
from app.schemas.receiving import (
    ApplyCorrectionsRequest,
    CompareInvoiceRequest,
    ConfirmReceivingRequest,
    DocumentUploadRequest,
    StartReceivingRequest,
)
from app.services.invoice_matching_service import match_invoice_items
from app.services.normalization import normalize_product_name, product_similarity


def start_receiving(db: Session, payload: StartReceivingRequest) -> Receiving:
    receiving = Receiving(
        request_id=payload.request_id,
        order_number=payload.order_number,
        venue=payload.venue,
        supplier=payload.supplier,
        delivery_address=payload.delivery_address,
        chat_id=payload.chat_id,
        user_id=payload.user_id,
        status=ReceivingStatus.receiving_started,
    )
    db.add(receiving)
    db.flush()

    for item in payload.order_items:
        db.add(
            OrderItemSnapshot(
                receiving_id=receiving.id,
                name=item.name,
                quantity=item.quantity,
                unit=item.unit,
                price=item.price,
                supplier_product_id=item.supplier_product_id,
                role=item.role,
            )
        )
    db.commit()
    db.refresh(receiving)
    return receiving


def add_document(db: Session, receiving_id: int, payload: DocumentUploadRequest) -> ReceivingDocument:
    receiving = _get_receiving(db, receiving_id)
    document = ReceivingDocument(
        receiving_id=receiving.id,
        file_id=payload.file_id,
        file_type=payload.file_type,
        source=payload.source,
        file_url=payload.file_url,
        raw_text=payload.raw_text,
        supplier_legal_name=payload.supplier_legal_name,
        invoice_number=payload.invoice_number,
        invoice_date=payload.invoice_date,
        ocr_status="uploaded" if not payload.raw_text else "ocr_processed",
    )
    receiving.status = ReceivingStatus.ocr_processed if payload.raw_text else ReceivingStatus.documents_uploaded
    db.add(document)
    db.commit()
    db.refresh(document)
    return document


def compare_invoice(db: Session, receiving_id: int, payload: CompareInvoiceRequest) -> Receiving:
    receiving = _get_receiving(db, receiving_id)

    for old_item in list(receiving.items):
        db.delete(old_item)
    db.flush()

    results = match_invoice_items(receiving.order_items, payload.items)
    for item in results:
        db.add(ReceivingItem(receiving_id=receiving.id, **item))

    _update_receiving_status_after_compare(receiving, results)
    _mark_supplier_mismatch_if_needed(receiving, payload.supplier_legal_name)

    if receiving.documents:
        document = receiving.documents[-1]
        document.invoice_number = payload.invoice_number or document.invoice_number
        document.invoice_date = payload.invoice_date or document.invoice_date
        document.supplier_legal_name = payload.supplier_legal_name or document.supplier_legal_name
        document.ocr_status = "ocr_processed"

    db.commit()
    db.refresh(receiving)
    return receiving


def apply_corrections(db: Session, receiving_id: int, payload: ApplyCorrectionsRequest) -> Receiving:
    receiving = _get_receiving(db, receiving_id)
    for correction in payload.corrections:
        item = _find_item_for_correction(receiving.items, correction.item_id, correction.item_query)
        if item is not None:
            if correction.action == "mark_received":
                item.status = ReceivingItemStatus.accepted
                if correction.quantity is not None:
                    item.received_quantity = correction.quantity
            elif correction.action == "reject":
                item.status = ReceivingItemStatus.rejected
            elif correction.action == "set_quantity" and correction.quantity is not None:
                item.received_quantity = correction.quantity
                item.status = ReceivingItemStatus.accepted
            elif correction.action == "set_price" and correction.price is not None:
                item.invoice_price = correction.price
                item.status = ReceivingItemStatus.accepted
            elif correction.action == "accept_extra":
                item.status = ReceivingItemStatus.extra_accepted
                if correction.quantity is not None:
                    item.received_quantity = correction.quantity
                item.comment = correction.comment or "Принято как дополнительная позиция"
            elif correction.action == "mark_crossed_out":
                item.status = ReceivingItemStatus.crossed_out
            elif correction.action == "manual_review":
                item.status = ReceivingItemStatus.manual_review
            else:
                item.status = ReceivingItemStatus.manual_review
            item.comment = correction.comment or item.comment
    _update_receiving_status_after_compare(
        receiving, [{"status": item.status} for item in receiving.items]
    )
    db.commit()
    db.refresh(receiving)
    return receiving


def confirm_receiving(db: Session, receiving_id: int, payload: ConfirmReceivingRequest) -> Receiving:
    receiving = _get_receiving(db, receiving_id)
    if payload.confirmed:
        receiving.status = ReceivingStatus.confirmed_partial if payload.partial else ReceivingStatus.confirmed_full
    else:
        receiving.status = ReceivingStatus.control_required
    receiving.comment = payload.comment
    db.commit()
    db.refresh(receiving)
    return receiving


def build_accounting_payload(receiving: Receiving) -> dict:
    document = receiving.documents[-1] if receiving.documents else None
    return {
        "requestId": receiving.request_id,
        "orderNumber": receiving.order_number,
        "venue": receiving.venue,
        "supplier": {
            "displayName": receiving.supplier,
            "legalName": document.supplier_legal_name if document else None,
        },
        "invoice": {
            "number": document.invoice_number if document else None,
            "date": document.invoice_date if document else None,
            "files": [doc.file_url or doc.file_id for doc in receiving.documents if doc.file_url or doc.file_id],
        },
        "items": [
            {
                "name": item.item_name_from_order or item.item_name_from_invoice or "",
                "orderedQuantity": item.ordered_quantity,
                "receivedQuantity": item.received_quantity,
                "unit": item.unit,
                "orderedPrice": item.ordered_price,
                "invoicePrice": item.invoice_price,
                "status": item.status.value,
                "comment": item.comment,
            }
            for item in receiving.items
            if item.status not in {ReceivingItemStatus.rejected, ReceivingItemStatus.crossed_out}
        ],
        "comment": receiving.comment,
    }


def _get_receiving(db: Session, receiving_id: int) -> Receiving:
    receiving = db.get(Receiving, receiving_id)
    if receiving is None:
        raise ValueError("Приемка не найдена")
    return receiving


def _update_receiving_status_after_compare(receiving: Receiving, results: list[dict]) -> None:
    statuses = {item["status"] for item in results}
    if ReceivingItemStatus.extra in statuses:
        receiving.status = ReceivingStatus.has_extra_items
    elif ReceivingItemStatus.missing in statuses:
        receiving.status = ReceivingStatus.matched_partial
    elif statuses <= {ReceivingItemStatus.matched}:
        receiving.status = ReceivingStatus.matched_full
    else:
        receiving.status = ReceivingStatus.requires_correction


def _mark_supplier_mismatch_if_needed(receiving: Receiving, invoice_supplier: str | None) -> None:
    if invoice_supplier:
        expected = normalize_product_name(receiving.supplier)
        actual = normalize_product_name(invoice_supplier)
        same_supplier = expected in actual or actual in expected
        similarity = product_similarity(expected, actual)
        if not same_supplier and similarity < 0.35:
            receiving.status = ReceivingStatus.requires_correction
            receiving.comment = (
                "Поставщик в накладной отличается от поставщика в заявке: "
                f"{invoice_supplier}"
            )


def _find_item_for_correction(items: list[ReceivingItem], item_id: int | None, query: str | None) -> ReceivingItem | None:
    result = None
    if item_id is not None:
        for item in items:
            if item.id == item_id:
                result = item
    elif query:
        query_norm = normalize_product_name(query)
        best_item = None
        best_score = 0.0
        for item in items:
            order_name = normalize_product_name(item.item_name_from_order)
            invoice_name = normalize_product_name(item.item_name_from_invoice)
            direct_match = query_norm in order_name or query_norm in invoice_name
            score = max(
                product_similarity(query_norm, order_name),
                product_similarity(query_norm, invoice_name),
            )
            if direct_match:
                best_item = item
                best_score = 1.0
            elif score > best_score:
                best_item = item
                best_score = score
        if best_score >= 0.25:
            result = best_item
    return result
