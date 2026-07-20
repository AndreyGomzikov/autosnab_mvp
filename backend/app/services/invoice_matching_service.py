from app.models.receiving import OrderItemSnapshot, ReceivingItemStatus
from app.schemas.receiving import InvoiceItemIn
from app.services.normalization import product_similarity

PRICE_TOLERANCE = 0.01
QUANTITY_TOLERANCE = 0.0001
MATCH_THRESHOLD = 0.55
REPLACEMENT_THRESHOLD = 0.25


def match_invoice_items(
    order_items: list[OrderItemSnapshot], invoice_items: list[InvoiceItemIn]
) -> list[dict]:
    results: list[dict] = []
    used_invoice_indexes: set[int] = set()

    for order_item in order_items:
        best_index = -1
        best_score = 0.0
        for index, invoice_item in enumerate(invoice_items):
            if index not in used_invoice_indexes:
                score = product_similarity(order_item.name, invoice_item.name)
                if score > best_score:
                    best_score = score
                    best_index = index

        if best_index == -1 or best_score < REPLACEMENT_THRESHOLD:
            results.append(_missing_result(order_item))
        else:
            invoice_item = invoice_items[best_index]
            used_invoice_indexes.add(best_index)
            results.append(_build_result(order_item, invoice_item, best_score))

    for index, invoice_item in enumerate(invoice_items):
        if index not in used_invoice_indexes:
            results.append(
                {
                    "item_name_from_order": None,
                    "item_name_from_invoice": invoice_item.name,
                    "ordered_quantity": 0,
                    "received_quantity": invoice_item.quantity,
                    "unit": invoice_item.unit,
                    "ordered_price": 0,
                    "invoice_price": invoice_item.price,
                    "status": ReceivingItemStatus.crossed_out if invoice_item.crossed_out else ReceivingItemStatus.extra,
                    "comment": invoice_item.comment or "Позиция есть в накладной, но отсутствует в заявке",
                }
            )
    return results


def _missing_result(order_item: OrderItemSnapshot) -> dict:
    return {
        "item_name_from_order": order_item.name,
        "item_name_from_invoice": None,
        "ordered_quantity": order_item.quantity,
        "received_quantity": 0,
        "unit": order_item.unit,
        "ordered_price": order_item.price,
        "invoice_price": 0,
        "status": ReceivingItemStatus.missing,
        "comment": "Товар из заявки не найден в накладной",
    }


def _build_result(order_item: OrderItemSnapshot, invoice_item: InvoiceItemIn, score: float) -> dict:
    status = ReceivingItemStatus.matched
    comment = invoice_item.comment

    if invoice_item.crossed_out:
        status = ReceivingItemStatus.crossed_out
        comment = comment or "Строка отмечена как зачеркнутая"
    elif score < MATCH_THRESHOLD:
        status = ReceivingItemStatus.replacement_candidate
        comment = comment or "Возможная замена товара"
    elif abs(order_item.quantity - invoice_item.quantity) > QUANTITY_TOLERANCE:
        status = ReceivingItemStatus.quantity_mismatch
        comment = comment or "Количество отличается от заявки"
    elif abs(order_item.price - invoice_item.price) > PRICE_TOLERANCE:
        status = ReceivingItemStatus.price_mismatch
        comment = comment or "Цена отличается от заявки"

    return {
        "item_name_from_order": order_item.name,
        "item_name_from_invoice": invoice_item.name,
        "ordered_quantity": order_item.quantity,
        "received_quantity": invoice_item.quantity,
        "unit": invoice_item.unit or order_item.unit,
        "ordered_price": order_item.price,
        "invoice_price": invoice_item.price,
        "status": status,
        "comment": comment,
    }
