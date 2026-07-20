import re


def normalize_product_name(value: str | None) -> str:
    text = (value or "").lower().replace("ё", "е")
    text = re.sub(r"[^a-zа-я0-9%.,\-\s]", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    replacements = {
        "гр.": "г",
        "гр": "г",
        "килограмм": "кг",
        "литр": "л",
        "штук": "шт",
    }
    for old, new in replacements.items():
        text = text.replace(old, new)
    return text


def product_similarity(left: str | None, right: str | None) -> float:
    left_norm = normalize_product_name(left)
    right_norm = normalize_product_name(right)
    if not left_norm or not right_norm:
        return 0.0
    left_tokens = set(left_norm.split())
    right_tokens = set(right_norm.split())
    token_score = len(left_tokens & right_tokens) / max(len(left_tokens | right_tokens), 1)
    substring_score = 1.0 if left_norm in right_norm or right_norm in left_norm else 0.0
    return max(token_score, substring_score)


_LATIN_TO_CYRILLIC = str.maketrans(
    {
        "A": "А",
        "B": "В",
        "C": "С",
        "E": "Е",
        "H": "Н",
        "K": "К",
        "M": "М",
        "O": "О",
        "P": "Р",
        "T": "Т",
        "X": "Х",
        "Y": "У",
    }
)


def canonical_invoice_number(value: str | None, *, document_form: str | None = None) -> str:
    text = (value or "").strip().upper()
    if not text:
        return ""
    text = re.sub(r"^\s*UPMK", "УПМК", text)
    text = re.sub(r"^\s*UT", "УТ", text)
    text = re.sub(r"^\s*UPD", "УПД", text)
    text = text.translate(_LATIN_TO_CYRILLIC)
    text = re.sub(r"[№#]", "", text)
    if _looks_like_receipt_number(text, document_form=document_form):
        text = re.sub(r"^\s*ЧЕК\s*", "", text)
    text = re.sub(r"\s+", "", text)
    text = re.sub(r"[^0-9A-ZА-ЯЁ\-/_]", "", text)
    return text


def _looks_like_receipt_number(value: str, *, document_form: str | None = None) -> bool:
    form = (document_form or "").strip().lower()
    normalized = value.lower()
    return "чек" in form or normalized.startswith("чек") or normalized.isdigit()
