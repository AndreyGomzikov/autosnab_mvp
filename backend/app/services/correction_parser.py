import re

from app.schemas.receiving import ApplyCorrectionsRequest, CorrectionIn

_NUMBER_WORDS = {
    "ноль": 0,
    "один": 1,
    "одна": 1,
    "одно": 1,
    "два": 2,
    "две": 2,
    "три": 3,
    "четыре": 4,
    "пять": 5,
    "шесть": 6,
    "семь": 7,
    "восемь": 8,
    "девять": 9,
    "десять": 10,
}


def parse_correction_text(text: str) -> ApplyCorrectionsRequest:
    """MVP-2 deterministic parser that imitates AI Agent JSON output.

    It does not update DB directly. It returns the same JSON-command shape that
    n8n/AI Agent is expected to send to backend after validation.
    """
    normalized = text.strip().lower()
    quantity = _extract_quantity(normalized)
    price = _extract_price(normalized)
    item_query = _extract_item_query(normalized)
    action = _detect_action(normalized, quantity, price)
    correction = CorrectionIn(
        item_query=item_query,
        action=action,
        quantity=quantity,
        price=price,
        comment=text,
    )
    return ApplyCorrectionsRequest(corrections=[correction])


def _detect_action(text: str, quantity: float | None, price: float | None) -> str:
    action = "mark_received"
    if any(word in text for word in ["не принимать", "отклонить", "отменить товар", "не было"]):
        action = "reject"
    elif any(word in text for word in ["зачерк", "вычерк"]):
        action = "mark_crossed_out"
    elif any(word in text for word in ["лишн", "дополнительн", "принять как дополнитель"]):
        action = "accept_extra"
    elif price is not None and any(word in text for word in ["цена", "стоимость", "руб"]):
        action = "set_price"
    elif quantity is not None:
        action = "set_quantity"
    return action


def _extract_quantity(text: str) -> float | None:
    result = None
    match = re.search(r"(\d+(?:[,.]\d+)?)\s*(?:шт|штук|кг|л|уп|короб|ед)?", text)
    if match:
        result = float(match.group(1).replace(",", "."))
    else:
        for word, value in _NUMBER_WORDS.items():
            if re.search(rf"\b{word}\b", text):
                result = float(value)
    return result


def _extract_price(text: str) -> float | None:
    result = None
    match = re.search(r"(?:цена|стоимость|по)\s*(\d+(?:[,.]\d+)?)", text)
    if match:
        result = float(match.group(1).replace(",", "."))
    return result


def _extract_item_query(text: str) -> str | None:
    cleaned = text
    patterns = [
        r"есть в накладной",
        r"пришло",
        r"пришел",
        r"пришла",
        r"принять",
        r"не принимать",
        r"отклонить",
        r"количество",
        r"цена",
        r"стоимость",
        r"строка\s*\d+",
        r"\d+(?:[,.]\d+)?\s*(?:шт|штук|кг|л|уп|короб|ед|руб|₽)?",
    ]
    for pattern in patterns:
        cleaned = re.sub(pattern, " ", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" .,:;—-")
    return cleaned or None
