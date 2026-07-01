import json
import mimetypes
import re
from pathlib import Path

from app.config import settings
from app.services.google_oauth_service import get_google_user_credentials


class OcrConfigurationError(RuntimeError):
    pass


DRIVE_OCR_SCOPES = ["https://www.googleapis.com/auth/drive"]
GOOGLE_DOC_MIME_TYPE = "application/vnd.google-apps.document"
TEXT_EXPORT_MIME_TYPE = "text/plain"
SUPPORTED_DRIVE_OCR_EXTENSIONS = {".jpg", ".jpeg", ".png", ".pdf", ".tif", ".tiff", ".bmp", ".gif"}


def recognize_invoice_image(file_path: str) -> dict:
    """Recognize invoice image/PDF through Google Drive OCR with OAuth user authorization.

    This implementation does not use Google Vision API and does not require
    Google Vision billing. It uploads the file to the authorized user's Google
    Drive as a temporary Google Docs document, exports the recognized text as
    plain text, and then removes the temporary document by default.
    """
    if not settings.google_drive_ocr_enabled:
        raise OcrConfigurationError(
            "Google Drive OCR отключен. Укажите GOOGLE_DRIVE_OCR_ENABLED=true."
        )
    return recognize_invoice_with_google_drive_ocr(file_path)


def recognize_invoice_with_google_drive_ocr(file_path: str) -> dict:
    path = Path(file_path)
    if not path.exists():
        raise OcrConfigurationError(f"Файл для OCR не найден: {file_path}")
    if path.suffix.lower() not in SUPPORTED_DRIVE_OCR_EXTENSIONS:
        raise OcrConfigurationError(
            "Google Drive OCR поддерживает JPG, JPEG, PNG, PDF, TIFF, BMP и GIF."
        )

    try:
        from googleapiclient.discovery import build
        from googleapiclient.http import MediaFileUpload
    except ImportError as exc:
        raise OcrConfigurationError(
            "Google Drive OCR: не установлены зависимости google-api-python-client/google-auth. Выполните pip install -r backend/requirements.txt."
        ) from exc

    credentials = get_google_user_credentials()
    drive_service = build("drive", "v3", credentials=credentials)

    mime_type = _guess_mime_type(path)
    media = MediaFileUpload(str(path), mimetype=mime_type, resumable=False)
    body = {
        "name": f"ocr_{path.stem}",
        "mimeType": GOOGLE_DOC_MIME_TYPE,
    }

    if settings.google_drive_ocr_folder_id:
        body["parents"] = [settings.google_drive_ocr_folder_id]

    document_id = None
    try:
        document = drive_service.files().create(
            body=body,
            media_body=media,
            fields="id,name",
            ocrLanguage=settings.google_drive_ocr_language,
            supportsAllDrives=True,
        ).execute()
        document_id = document["id"]

        exported = drive_service.files().export(
            fileId=document_id,
            mimeType=TEXT_EXPORT_MIME_TYPE,
        ).execute()
        raw_text = exported.decode("utf-8", errors="replace") if isinstance(exported, bytes) else str(exported)
    finally:
        if document_id and settings.google_drive_ocr_delete_temp_files:
            _delete_drive_file_safely(drive_service, document_id)

    return {
        "provider": "google_drive_ocr",
        "raw_text": raw_text,
        "confidence": None,
        "pages": None,
        "temporary_document_id": None if settings.google_drive_ocr_delete_temp_files else document_id,
    }


def parse_invoice_text_to_payload(raw_text: str, fallback_filename: str | None = None) -> dict:
    """Small deterministic parser for MVP-4.

    Real OCR extracts text; this parser creates a first draft that the user verifies
    in Google Sheets. It does not pretend to be perfect and marks uncertain fields.
    """
    text = raw_text or ""
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    joined = "\n".join(lines)
    is_upd_document = _looks_like_upd_document(lines)
    is_torg12_continuation_page = (
        not is_upd_document
        and _looks_like_torg12_continuation_page(lines)
    )

    if is_torg12_continuation_page:
        # Страница-продолжение ТОРГ-12 не содержит шапку документа.
        # Не подставляем в шапку значения из подписей, массы груза или
        # получателя внизу страницы: берём только товарные строки и явный итог.
        supplier = None
        invoice_number = None
        invoice_date = None
        venue = None
        store = None
        consignee = None
        recipient = None
        supplier_inn = None
    else:
        supplier = (_extract_upd_supplier(lines) if is_upd_document else None) or _extract_supplier(lines)
        invoice_number = (_extract_upd_invoice_number(lines) if is_upd_document else None) or _extract_invoice_number(joined)
        invoice_date = (_extract_upd_invoice_date(lines) if is_upd_document else None) or _extract_date(joined)
        venue = _extract_delivery_point(lines)
        store = _extract_store_or_department(lines)
        consignee = _extract_party_by_label(lines, "грузополучатель")
        recipient = _extract_recipient(lines) or consignee or venue
        supplier_inn = (
            _extract_upd_supplier_inn(lines, supplier)
            if is_upd_document
            else None
        ) or _extract_supplier_inn(lines, supplier)

    items = _extract_items(lines)
    total_sum = _extract_invoice_total(joined, items)
    return {
        "supplier": supplier,
        "supplier_legal_name": supplier,
        "invoice_number": invoice_number,
        "invoice_date": invoice_date,
        "document_form": _extract_document_form(joined),
        "supplier_inn": supplier_inn,
        "consignee": consignee,
        "recipient": recipient,
        "trade_point": venue,
        "warehouse": store,
        "basis": None if is_torg12_continuation_page else _extract_basis(lines),
        "venue": venue,
        "delivery_address": venue,
        "store": store,
        "display_store": store,
        "iiko_default_store_id": store,
        "total_sum": total_sum,
        "raw_text": raw_text,
        "items": items,
        "parser_notes": [
            "Поля распознаны автоматически через Google Drive OCR и должны быть проверены пользователем в Google Таблице.",
            "Если товарные строки не распознаны, заполните их вручную в таблице перед отправкой.",
        ],
    }



def _extract_document_form(text: str) -> str | None:
    lowered = (text or "").lower()
    if "универсаль" in lowered and "передаточ" in lowered:
        return "УПД"
    if "торг-12" in lowered or "товарная накладная" in lowered:
        return "ТОРГ-12"
    if "счет-фактура" in lowered or "счёт-фактура" in lowered:
        return "Счет-фактура"
    if "накладная" in lowered:
        return "Накладная"
    return None


def _extract_upd_invoice_number(lines: list[str]) -> str | None:
    normalized_lines = [_normalize_line(line) for line in lines]
    joined = "\n".join(normalized_lines[:140])
    for match in re.finditer(r"(?:№|no|n)\s*([A-Za-zА-Яа-яЁ]{1,8}\s*[-–—]?\s*\d{2,})", joined, flags=re.IGNORECASE):
        context_start = max(0, match.start() - 120)
        context = joined[context_start : match.end() + 80].lower()
        if "постанов" in context or "приложение" in context:
            continue
        value = _normalize_document_number(match.group(1))
        if value:
            return value

    contexts = []
    for index, line in enumerate(normalized_lines[:100]):
        lowered = line.lower()
        if any(marker in lowered for marker in ("счет-фактура", "счёт-фактура", "документ об отгрузке", "универсальный передаточный документ")):
            context = " ".join(normalized_lines[index : index + 8])
            if "постанов" not in context.lower() and "приложение" not in context.lower():
                contexts.append(context)

    for context in contexts:
        value = _extract_document_number_candidate(context)
        if value:
            return value
    return None


def _extract_upd_invoice_date(lines: list[str]) -> str | None:
    normalized_lines = [_normalize_line(line) for line in lines]
    priority_markers = ("счет-фактура", "счёт-фактура", "документ об отгрузке")
    secondary_markers = ("универсальный передаточный документ",)

    for markers in (priority_markers, secondary_markers):
        for index, line in enumerate(normalized_lines[:120]):
            lowered = line.lower()
            if not any(marker in lowered for marker in markers):
                continue
            context = " ".join(normalized_lines[index : index + 10])
            context_lower = context.lower()
            if "постанов" in context_lower or "приложение" in context_lower:
                continue
            value = _extract_date_from_text(context)
            if value:
                return value
    return None


def _extract_date_from_text(text: str) -> str | None:
    months = {
        "января": "01",
        "янв": "01",
        "февраля": "02",
        "фев": "02",
        "марта": "03",
        "мар": "03",
        "апреля": "04",
        "апр": "04",
        "мая": "05",
        "май": "05",
        "июня": "06",
        "июн": "06",
        "июля": "07",
        "июл": "07",
        "августа": "08",
        "авг": "08",
        "сентября": "09",
        "сент": "09",
        "сен": "09",
        "октября": "10",
        "окт": "10",
        "ноября": "11",
        "ноя": "11",
        "декабря": "12",
        "дек": "12",
    }
    month_pattern = "|".join(sorted(months, key=len, reverse=True))
    match = re.search(rf"\b(\d{{1,2}})[\s.-]+({month_pattern})[\s.-]+(\d{{2,4}})\b", text, flags=re.IGNORECASE)
    if match:
        day = int(match.group(1))
        month = months[match.group(2).lower()]
        raw_year = int(match.group(3))
        year = raw_year if raw_year >= 100 else 2000 + raw_year
        return f"{year}-{month}-{day:02d}"

    match = re.search(r"\b(\d{1,2})[./-](\d{1,2})[./-](\d{2,4})\b", text)
    if match:
        day = int(match.group(1))
        month = int(match.group(2))
        raw_year = int(match.group(3))
        year = raw_year if raw_year >= 100 else 2000 + raw_year
        return f"{year}-{month:02d}-{day:02d}"
    return None


def _extract_upd_supplier(lines: list[str]) -> str | None:
    organization_markers = _organization_markers()
    seller_label_indexes = []
    for index, raw_line in enumerate(lines[:90]):
        line = _normalize_line(raw_line)
        lowered = line.lower()
        if not re.search(r"(?<![а-яё])продавец(?![а-яё])", lowered):
            continue
        seller_label_indexes.append(index)
        same_line = re.sub(r"(?i)^.*?продавец\s*[:\-—]?\s*", "", line, count=1)
        same_line = _cut_upd_party_at_next_label(same_line)
        selected = _select_party_from_ocr_line(same_line, "продавец")
        if selected:
            return selected
        cleaned = _clean_party_value(same_line)
        if cleaned and _line_has_organization(cleaned, organization_markers):
            return cleaned

        for candidate in lines[index + 1 : index + 10]:
            candidate_text = _normalize_line(candidate)
            candidate_lower = candidate_text.lower()
            if any(label in candidate_lower for label in ("инн/кпп продавца", "грузоотправитель", "грузополучатель", "покупатель", "платежно-расчетному", "документ об отгрузке")):
                break
            if candidate_lower in {"адрес", "адрес:"}:
                continue
            selected = _select_party_from_ocr_line(candidate_text, "продавец")
            if selected:
                return selected
            cleaned = _clean_party_value(candidate_text)
            if cleaned and _line_has_organization(cleaned, organization_markers):
                return cleaned

    # В некоторых УПД Google OCR читает правую верхнюю часть страницы раньше
    # левой: сначала попадает покупатель, затем строка продавца, а печатная
    # метка "Продавец:" оказывается ниже самой организации. Поэтому ищем
    # ближайшую организацию над меткой продавца.
    for index in seller_label_indexes:
        selected = _extract_upd_supplier_before_label(lines, index)
        if selected:
            return selected
    return None


def _extract_upd_supplier_before_label(lines: list[str], label_index: int) -> str | None:
    organization_markers = _organization_markers()
    for candidate in reversed(lines[max(0, label_index - 18) : label_index]):
        candidate_text = _normalize_line(candidate)
        lowered = candidate_text.lower()
        if not _line_has_organization(candidate_text, organization_markers):
            continue
        if any(marker in lowered for marker in ("покупатель", "грузополучатель", "валюта", "российский рубль")):
            continue
        cleaned = _clean_party_value(candidate_text)
        if cleaned and _line_has_organization(cleaned, organization_markers):
            return cleaned
    return None


def _extract_upd_supplier_inn(lines: list[str], supplier: str | None = None) -> str | None:
    supplier_inn = _extract_inn_from_text(supplier)
    if supplier_inn:
        return supplier_inn

    normalized_lines = [_normalize_line(line) for line in lines]
    for index, line in enumerate(normalized_lines[:100]):
        lowered = line.lower()
        if not any(label in lowered for label in ("инн/кпп продавца", "инн продавца", "инн/кпп продавца.")):
            continue
        for candidate in normalized_lines[index : index + 6]:
            value = _extract_plain_inn(candidate)
            if value:
                return value
    return None


def _extract_plain_inn(value: str | None) -> str | None:
    if not value:
        return None
    match = re.search(r"\b(\d{10}|\d{12})\s*/\s*\d{9}\b", value)
    if match:
        return match.group(1)
    match = re.search(r"\b(\d{10}|\d{12})\b", value)
    return match.group(1) if match else None


def _cut_upd_party_at_next_label(value: str) -> str:
    return re.split(
        r"(?i)\b(?:покупатель|грузоотправитель|грузополучатель|инн/кпп|документ об отгрузке|валюта)\b",
        value,
        maxsplit=1,
    )[0].strip(" ,;:-—")


def _extract_supplier_inn(lines: list[str], supplier: str | None) -> str | None:
    supplier_inn = _extract_inn_from_text(supplier)
    if supplier_inn:
        return supplier_inn

    for index, raw_line in enumerate(lines[:60]):
        line = _normalize_line(raw_line)
        if all(label not in line.lower() for label in ("поставщик", "продавец", "продавца")):
            continue
        for candidate in [line, *lines[index + 1 : index + 8]]:
            candidate_text = _normalize_line(candidate)
            if any(label in candidate_text.lower() for label in ("грузополучатель", "получатель", "покупатель", "плательщик", "основание")):
                break
            supplier_inn = _extract_inn_from_text(candidate_text)
            if supplier_inn:
                return supplier_inn
    return None


def _extract_inn_from_text(value: str | None) -> str | None:
    if not value:
        return None
    match = re.search(r"\bИНН\s*[:№]?\s*(\d{10,12})\b", value, flags=re.IGNORECASE)
    return match.group(1) if match else None


def _extract_recipient(lines: list[str]) -> str | None:
    for label in ("получатель", "покупатель"):
        value = _extract_party_by_exact_label(lines, label)
        if value:
            return value
    return None


def _extract_party_by_exact_label(lines: list[str], label: str) -> str | None:
    organization_markers = _organization_markers()
    pattern = re.compile(rf"(?<![а-яё]){re.escape(label)}(?![а-яё])", flags=re.IGNORECASE)
    for index, raw_line in enumerate(lines[:70]):
        line = _normalize_line(raw_line)
        if not pattern.search(line):
            continue
        same_line = pattern.sub("", line, count=1).strip(" :-—\t")
        cleaned = _clean_party_value(same_line)
        if cleaned and _line_has_organization(cleaned, organization_markers):
            return cleaned
        for candidate in lines[index + 1 : index + 6]:
            candidate_text = _normalize_line(candidate)
            lowered = candidate_text.lower()
            if any(next_label in lowered for next_label in ("грузоотправитель", "грузополучатель", "поставщик", "плательщик", "основание")):
                break
            cleaned = _clean_party_value(candidate_text)
            if cleaned and _line_has_organization(cleaned, organization_markers):
                return cleaned
    return None


def _extract_basis(lines: list[str]) -> str | None:
    upd_basis = _extract_upd_basis(lines)
    if upd_basis:
        return upd_basis

    for index, raw_line in enumerate(lines[:140]):
        line = _normalize_line(raw_line)
        lowered = line.lower()
        if "основание" not in lowered:
            continue
        same_line = re.sub(r"(?i)^.*?основание\s*[:\-—]?\s*", "", line, count=1).strip()
        cleaned = _clean_basis_value(same_line)
        if cleaned:
            return cleaned
        for candidate in lines[index + 1 : index + 14]:
            candidate_text = _normalize_line(candidate)
            candidate_lower = candidate_text.lower()
            if any(label in candidate_lower for label in ("поставщик", "грузополучатель", "получатель", "товар (груз)", "главный бухгалтер")):
                break
            cleaned = _clean_basis_value(candidate_text)
            if cleaned:
                return cleaned
    return None


def _extract_upd_basis(lines: list[str]) -> str | None:
    if not _looks_like_upd_document(lines):
        return None

    normalized_lines = [_normalize_line(line) for line in lines]
    start_index = None
    for index, line in enumerate(normalized_lines[:220]):
        lowered = line.lower()
        if "основание" in lowered and ("передачи" in lowered or "получения" in lowered or "прием" in lowered or "приём" in lowered):
            start_index = index
            break

    if start_index is not None:
        fallback = None
        for candidate in normalized_lines[start_index + 1 : start_index + 120]:
            lowered = candidate.lower()
            if any(marker in lowered for marker in ("товар (груз) передал", "товар (груз) получил", "ответственный за правильность")):
                break
            cleaned = _clean_basis_value(candidate)
            if not cleaned:
                continue
            if "договор" in cleaned.lower():
                return cleaned
            if fallback is None:
                fallback = cleaned
        if fallback:
            return fallback

    # В реальных сканах УПД строка договора может быть распознана над самой
    # меткой "Основание передачи...". Поэтому после основного прохода ищем
    # договор по всему документу, отбрасывая печатные подсказки формы.
    return _find_upd_basis_contract_anywhere(normalized_lines)


def _find_upd_basis_contract_anywhere(lines: list[str]) -> str | None:
    priority_lines = [line for line in lines[:240] if line.lower().lstrip().startswith("договор")]
    fallback_lines = [line for line in lines[:240] if "договор" in line.lower()]
    for line in [*priority_lines, *fallback_lines]:
        lowered = line.lower()
        if "договор" not in lowered:
            continue
        cleaned = _clean_basis_value(line)
        if cleaned and "договор" in cleaned.lower():
            return cleaned
    return None


def _clean_basis_value(value: str | None) -> str | None:
    if not value:
        return None
    cleaned = _normalize_line(value)
    cleaned = _remove_torg12_service_hints(cleaned)
    cleaned = re.sub(r"(?i)^основание\s*[:\-—]?\s*", "", cleaned).strip(" ;,.-—")
    if not cleaned:
        return None
    if _looks_like_basis_service_label(cleaned):
        return None
    if _looks_like_header_or_total(cleaned):
        return None
    if _is_service_hint_line(cleaned) and "договор" not in cleaned.lower():
        return None
    return cleaned


def _looks_like_basis_service_label(value: str) -> bool:
    lowered = value.lower().strip(" ()[]")
    if not lowered:
        return True
    service_markers = (
        "передачи",
        "сдачи",
        "получения",
        "приемки",
        "приёмки",
        "данные о транспортировке",
        "договор, доверенность",
        "транспортная накладная",
        "экспедиторская",
        "складская расписка",
        "основной государственный",
        "индивидуального предпринимателя",
        "идентификатор государственного контракта",
        "договора (соглашения)",
        "код вида товара",
        "единица измерения",
        "условное обозна",
        "наименование товара",
        "сумма налога",
        "налоговая ставка",
        "стоимость товаров",
        "страна происхождения",
        "регистрационный номер",
        "всего к оплате",
    )
    return any(marker in lowered for marker in service_markers)


def _guess_mime_type(path: Path) -> str:
    mime_type, _ = mimetypes.guess_type(path.name)
    if mime_type:
        return mime_type
    if path.suffix.lower() == ".pdf":
        return "application/pdf"
    return "application/octet-stream"


def _delete_drive_file_safely(drive_service, file_id: str) -> None:
    try:
        drive_service.files().delete(fileId=file_id, supportsAllDrives=True).execute()
    except Exception:
        pass


def _extract_supplier(lines: list[str]) -> str | None:
    organization_markers = _organization_markers()

    for index, raw_line in enumerate(lines[:60]):
        line = _normalize_line(raw_line)
        lowered = line.lower()
        if "поставщик" not in lowered and "продавец" not in lowered:
            continue

        match = re.search("поставщик", line, flags=re.IGNORECASE)
        before_label = _extract_party_candidate_from_prefix(line[: match.start()] if match else "", prefer_last=True)
        if before_label and not _looks_like_bank_fragment(before_label):
            return before_label

        explicit_value = re.sub(r"(?i).*?(?:поставщик|продавец)\s*[:\-—]?\s*", "", line).strip()
        selected = _select_party_from_ocr_line(explicit_value, "поставщик")
        if selected and not _looks_like_bank_fragment(selected):
            return selected
        if explicit_value and _line_has_organization(explicit_value, organization_markers) and not _looks_like_bank_fragment(explicit_value):
            return _normalize_organization_name(explicit_value)
        for candidate in lines[index + 1 : index + 8]:
            candidate_text = _normalize_line(candidate)
            candidate_lower = candidate_text.lower()
            if "основание" in candidate_lower:
                break
            if any(label in candidate_lower for label in ("плательщик", "грузоотправитель", "структурное")):
                continue
            selected = _select_party_from_ocr_line(candidate_text, "поставщик")
            if selected and not _looks_like_bank_fragment(selected):
                return selected
            if "грузополучатель" in candidate_lower:
                continue
            if _line_has_organization(candidate_text, organization_markers) and not _looks_like_bank_fragment(candidate_text):
                return _normalize_organization_name(candidate_text)

    if _looks_like_torg12_continuation_page(lines):
        return None

    for line in lines[:30]:
        normalized = _normalize_line(line)
        selected = _select_party_from_ocr_line(normalized, "поставщик")
        if selected and not _looks_like_bank_fragment(selected):
            return selected
        if _line_has_organization(normalized, organization_markers) and not _looks_like_bank_fragment(normalized):
            return _normalize_organization_name(normalized)
    return None


def _looks_like_torg12_continuation_page(lines: list[str]) -> bool:
    joined = "\n".join(_normalize_line(line).lower() for line in lines[:220])
    has_page_two_marker = bool(re.search(r"страница\s*2\b", joined))
    has_split_page_two_marker = bool(re.search(r"страница\s*\n\s*2\b", joined))
    has_continuation_text = any(
        marker in joined
        for marker in (
            "товарная накладная имеет приложение",
            "порядковых номеров записей",
            "всего отпущено на сумму",
        )
    )
    has_continuation_marker = has_page_two_marker or has_split_page_two_marker or has_continuation_text
    has_full_header = (
        "номер документа" in joined
        and "дата составления" in joined
        and any(label in joined for label in ("поставщик", "грузополучатель", "плательщик"))
    )
    return has_continuation_marker and not has_full_header

def _extract_delivery_point(lines: list[str]) -> str | None:
    """Extract the user-facing delivery point from TORG-12 header.

    For Russian TORG-12 forms the best business value for "Заведение / точка
    доставки" is usually the party in the "Грузополучатель" line. If OCR
    misses it, we try "Покупатель" as a weaker fallback. No service placeholder
    is returned: missing value stays empty in the Google Sheet.
    """
    return _extract_party_by_label(lines, "грузополучатель") or _extract_party_by_label(lines, "покупатель")


def _extract_store_or_department(lines: list[str]) -> str | None:
    """Extract warehouse/department only if it is explicitly present in OCR text.

    Many TORG-12 samples contain the printed hint "структурное подразделение"
    but leave the actual field empty. In that case we return None, so the
    Google Sheet cell remains empty instead of showing service text.
    """
    for index, raw_line in enumerate(lines[:40]):
        line = _normalize_line(raw_line)
        lowered = line.lower()
        if not any(label in lowered for label in ("склад", "подразделение")):
            continue
        if _is_service_hint_line(line):
            continue

        same_line = re.sub(r"(?i)^.*?(?:склад\s*/\s*подразделение|склад|подразделение)\s*[:\-—]?\s*", "", line, count=1).strip()
        cleaned = _clean_header_field_value(same_line)
        if cleaned:
            return cleaned

        for candidate in lines[index + 1 : index + 4]:
            cleaned = _clean_header_field_value(candidate)
            if cleaned:
                return cleaned
    return None


def _extract_party_by_label(lines: list[str], label: str) -> str | None:
    organization_markers = _organization_markers()
    for index, raw_line in enumerate(lines[:60]):
        line = _normalize_line(raw_line)
        lowered = line.lower()
        if label not in lowered:
            continue

        # Google Drive OCR sometimes places the field label inside a long
        # party line, for example:
        #   ООО "Покупатель" ... ЗАО Грузополучатель "Банк" ...
        # In that case the business value is before the label, not after it.
        before_label = _party_value_before_embedded_label(line, label)
        if before_label and _line_has_organization(before_label, organization_markers):
            return before_label

        same_line = re.sub(rf"(?i)^.*?{re.escape(label)}\s*[:\-—]?\s*", "", line, count=1).strip()
        selected = _select_party_from_ocr_line(same_line, label)
        if selected and not _looks_like_bank_fragment(selected):
            return selected
        cleaned = _clean_party_value(same_line)
        if cleaned and _line_has_organization(cleaned, organization_markers) and not _looks_like_bank_fragment(cleaned):
            return cleaned

        for candidate in lines[index + 1 : index + 6]:
            candidate_text = _normalize_line(candidate)
            candidate_lower = candidate_text.lower()
            if any(next_label in candidate_lower for next_label in ("грузоотправитель", "грузополучатель", "поставщик", "плательщик", "основание")):
                # If the next label is embedded in the same OCR line, try to
                # recover the party before this embedded label before stopping.
                embedded = _party_value_before_any_label(candidate_text)
                if embedded and _line_has_organization(embedded, organization_markers):
                    return embedded
                break
            selected = _select_party_from_ocr_line(candidate_text, label)
            if selected and not _looks_like_bank_fragment(selected):
                return selected
            cleaned = _clean_party_value(candidate_text)
            if cleaned and _line_has_organization(cleaned, organization_markers) and not _looks_like_bank_fragment(cleaned):
                return cleaned
    return None



def _split_primary_organization_chunks(value: str | None) -> list[str]:
    """Split an OCR line that contains several business parties.

    Google Drive OCR can collapse labels and values into one line, for example:
    ООО Балтика ... ООО Ресторан «Петр I» ...
    For header fields we need separate parties, not the whole mixed line.
    """
    if not value:
        return []
    normalized = _normalize_line(value)
    matches = list(re.finditer(r'(?<![А-Яа-яЁё0-9])(?:ООО|000|ИП)\s*(?:["«]|\b)', normalized))
    if not matches:
        return []
    chunks = []
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(normalized)
        chunk = normalized[match.start() : end].strip(" ,;:-—")
        cleaned = _clean_party_value(chunk)
        if cleaned:
            chunks.append(cleaned)
    return chunks


def _select_party_from_ocr_line(value: str | None, label: str) -> str | None:
    chunks = _split_primary_organization_chunks(value)
    if not chunks:
        return None
    normalized_label = label.lower()
    non_bank_chunks = [chunk for chunk in chunks if not _looks_like_bank_fragment(chunk)]
    usable_chunks = non_bank_chunks or chunks
    if normalized_label in {"грузополучатель", "покупатель"}:
        return usable_chunks[0]
    if normalized_label == "поставщик":
        return usable_chunks[1] if len(usable_chunks) >= 2 else usable_chunks[0]
    return usable_chunks[0]


def _party_value_before_any_label(value: str) -> str | None:
    labels = ("грузополучатель", "покупатель", "поставщик", "плательщик", "основание")
    lowered = value.lower()
    positions = [(lowered.find(label), label) for label in labels if lowered.find(label) > 0]
    if not positions:
        return None
    positions.sort()
    return _party_value_before_embedded_label(value, positions[0][1])


def _party_value_before_embedded_label(value: str, label: str) -> str | None:
    match = re.search(re.escape(label), value, flags=re.IGNORECASE)
    if not match or match.start() <= 0:
        return None

    before = value[: match.start()].strip(" ,;:-—")
    after = value[match.end() :].strip(" ,;:-—")
    candidate = _extract_party_candidate_from_prefix(before)
    if not candidate:
        return None

    # If OCR split the bank name by inserting the label after "ЗАО/АО",
    # keep the short bank continuation after the label until the printed
    # service hint. This reconstructs: ЗАО + "Банк" г. Москва, БИК...
    continuation = _party_continuation_after_embedded_label(after)
    if continuation and re.search(r"(?:\bЗАО\b|\bАО\b)\s*$", candidate):
        candidate = f"{candidate} {continuation}"

    return _clean_party_value(candidate)


def _extract_party_candidate_from_prefix(value: str, prefer_last: bool = False) -> str | None:
    if not value:
        return None
    normalized = _normalize_line(value)
    spans = _find_party_candidate_spans(normalized)
    if not spans:
        return None
    span_index = len(spans) - 1 if prefer_last else 0
    start = spans[span_index].start()
    end = spans[span_index + 1].start() if span_index + 1 < len(spans) else len(normalized)
    candidate = normalized[start:end].strip(" ,;:-—")
    candidate = _trim_party_bank_details(candidate)
    return candidate or None


def _find_party_candidate_spans(value: str) -> list[re.Match]:
    patterns = (
        r'(?<![А-Яа-яЁё0-9])(?:ООО|000|ИП)\s*(?:["«]|[A-Za-zА-Яа-яЁё])',
        r'(?<![А-Яа-яЁё0-9])[A-ZА-ЯЁ][A-Za-zА-Яа-яЁё0-9"«»._ -]{1,60}\s+ООО\b',
        r'(?<![А-Яа-яЁё0-9])(?:АО|ЗАО)\s*(?:["«]|[A-Za-zА-Яа-яЁё])',
    )
    matches = []
    for pattern in patterns:
        matches.extend(re.finditer(pattern, value))
    matches.sort(key=lambda match: match.start())

    filtered = []
    occupied_until = -1
    for match in matches:
        if match.start() < occupied_until:
            continue
        filtered.append(match)
        occupied_until = match.end()
    return filtered


def _trim_party_bank_details(value: str) -> str:
    cleaned = _normalize_line(value)
    cleaned = re.split(
        r"(?i)\s+(?:р/с|р\\с|в\s+банке|бик|к/с|организация|телефон|факс|банковские\s+реквизиты|адрес\s+доставки)\b",
        cleaned,
        maxsplit=1,
    )[0]
    return cleaned.strip(" ,;:-—")


def _party_continuation_after_embedded_label(value: str) -> str | None:
    if not value:
        return None
    cleaned = _cut_at_service_hint(value)
    cleaned = re.split(
        r"(?i)\s+(?:грузоотправитель|грузополучатель|поставщик|плательщик|основание)\b",
        cleaned,
        maxsplit=1,
    )[0]
    cleaned = cleaned.strip(" ,;:-—")
    if not cleaned:
        return None
    if not re.search(r"банк|бик|к/с|р/с", cleaned, flags=re.IGNORECASE):
        return None
    return cleaned


def _cut_at_service_hint(value: str) -> str:
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
    pattern = r"\s*\([^)]*(?:" + "|".join(service_words) + r")[^)]*\).*"
    return re.sub(pattern, "", value, flags=re.IGNORECASE).strip()


def _looks_like_bank_fragment(value: str) -> bool:
    lowered = value.lower().strip(' "«»')
    if lowered.startswith("банк") or lowered.startswith("зао банк") or lowered.startswith("зао \"банк"):
        return True
    if lowered.startswith("филиал") and "банк" in lowered:
        return True
    if (" банк " in f" {lowered} " or "банке" in lowered) and "бик" in lowered and not lowered.startswith(("ооо", "000", "ип ")):
        return True
    return False

def _extract_invoice_total(text: str, items: list[dict] | None = None) -> float | None:
    """Extract total amount with VAT from the invoice or calculate it from items."""
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if _looks_like_cash_receipt(lines):
        receipt_total = _extract_cash_receipt_total(lines)
        if receipt_total is not None:
            return receipt_total

    continuation_total = _extract_torg12_continuation_total(lines)
    if continuation_total is not None:
        return continuation_total

    explicit_total = _extract_explicit_invoice_total(lines, text)
    if explicit_total is not None:
        return explicit_total

    if items:
        total = sum(float(item.get("sum") or 0) + float(item.get("vat_sum") or 0) for item in items)
        if total > 0:
            return round(total, 2)

    receipt_total = _extract_cash_receipt_total(lines)
    if receipt_total is not None:
        return receipt_total

    return None



def _extract_torg12_continuation_total(lines: list[str]) -> float | None:
    """Return the visible document total from a continuation page of TORG-12.

    A second page can have only one product row. Summing parsed items then gives
    the row total, while the printed "Итого" line contains the full invoice
    amount. We intentionally use this only for continuation pages, so ordinary
    headers with bank account numbers are not affected.
    """
    if not _looks_like_torg12_continuation_page(lines):
        return None

    normalized_lines = [_normalize_line(line) for line in lines]
    start_index = None
    for index, line in enumerate(normalized_lines):
        lowered = line.lower()
        if lowered == "итого" or lowered.startswith("итого "):
            start_index = index
            break
    if start_index is None:
        start_index = _find_items_table_start(normalized_lines)

    end_index = len(normalized_lines)
    for index in range(start_index + 1, len(normalized_lines)):
        lowered = normalized_lines[index].lower()
        if "всего отпущено на сумму" in lowered:
            end_index = index
            break

    amount_candidates = []
    for line in normalized_lines[start_index:end_index]:
        if "двадцать" in line.lower() or "двенадцать" in line.lower():
            continue
        for value in _extract_numbers_from_text(line):
            if value >= 1000:
                amount_candidates.append(value)
    if amount_candidates:
        return max(amount_candidates)
    return None


def _extract_explicit_invoice_total(lines: list[str], text: str) -> float | None:
    """Find a printed document total before falling back to summing items.

    A second page of TORG-12 can contain only one product row, but the printed
    page total still refers to the whole delivery note. In that case summing the
    parsed rows gives the page-row amount, not the invoice amount, so explicit
    total rows have higher priority.
    """
    total_markers = (
        "всего по накладной",
        "всего отпущено на сумму",
    )
    for index, line in enumerate(lines):
        lowered = _normalize_line(line).lower()
        if not any(marker in lowered for marker in total_markers) and lowered != "итого":
            continue
        candidates = []
        for candidate in lines[index : index + 10]:
            candidates.extend(_extract_numbers_from_text(candidate))
        money_candidates = [value for value in candidates if value >= 100]
        if money_candidates:
            return money_candidates[-1]

    patterns = (
        r"всего\s+по\s+накладной[\s\S]{0,120}?(\d[\d\s]*(?:[,.]\d+)?)",
        r"всего\s+отпущено\s+на\s+сумму[\s\S]{0,160}?(\d[\d\s]*(?:[,.]\d+)?)",
        r"итого[\s\S]{0,120}?(\d[\d\s]*(?:[,.]\d+)?)",
    )
    for pattern in patterns:
        matches = list(re.finditer(pattern, text, flags=re.IGNORECASE))
        for match in reversed(matches):
            try:
                value = _to_float(match.group(1))
            except ValueError:
                continue
            if value > 0:
                return value
    return None



def _extract_cash_receipt_total(lines: list[str]) -> float | None:
    for index in range(len(lines) - 1, -1, -1):
        line = _normalize_line(lines[index])
        lowered = line.lower()
        if "итого" not in lowered:
            continue
        values = _extract_numbers_from_text(line)
        if not values and index + 1 < len(lines):
            values = _extract_numbers_from_text(lines[index + 1])
        if values:
            return values[-1]
    return None


def _clean_party_value(value: str | None) -> str | None:
    if value:
        value = _trim_party_bank_details(_cut_at_service_hint(value))
    cleaned = _clean_header_field_value(value)
    if not cleaned:
        return None
    cleaned = re.sub(r'(?<![А-Яа-яЁё])000(?=\s*["«])', 'ООО', cleaned)
    return _normalize_organization_name(cleaned)


def _clean_header_field_value(value: str | None) -> str | None:
    if not value:
        return None
    cleaned = _normalize_line(value)
    cleaned = _remove_torg12_service_hints(cleaned)
    cleaned = re.sub(r"(?i)^(?:грузоотправитель|грузополучатель|поставщик|плательщик|склад\s*/\s*подразделение|склад|подразделение)\s*[:\-—]?\s*", "", cleaned).strip()
    cleaned = cleaned.strip(" ;,.-—")
    if not cleaned:
        return None
    if _is_service_hint_line(cleaned):
        return None
    if _looks_like_header_or_total(cleaned):
        return None
    return cleaned


def _is_service_hint_line(value: str) -> bool:
    lowered = value.lower().strip(" ()")
    service_markers = (
        "организация",
        "адрес",
        "телефон",
        "факс",
        "банковские реквизиты",
        "структурное подразделение",
        "договор",
        "заказ",
        "расшифровка",
        "подпись",
        "должность",
    )
    return any(marker in lowered for marker in service_markers) and len(lowered) < 140


def _organization_markers() -> tuple[str, ...]:
    return (
        "ООО",
        "ИП ",
        "АО ",
        "ЗАО",
        "Общество с ограниченной ответственностью",
        "общество с ограниченной ответственностью",
    )


def _line_has_organization(value: str, markers: tuple[str, ...]) -> bool:
    return any(marker in value for marker in markers) or bool(re.search(r'(?<![А-Яа-яЁё])000\s*["«]', value))


def _extract_invoice_number(text: str) -> str | None:
    lines = [line.strip() for line in text.splitlines() if line.strip()]

    # ТОРГ-12 часто распознаётся так:
    #   Номер документа
    #   Дата составления
    #   3-0517-2
    #   17 мая 2014
    # или одной строкой: "Номер документа 645".
    for index, line in enumerate(lines):
        lowered = line.lower()
        if "номер документа" in lowered:
            same_line = re.sub(r"(?i).*?номер\s+документа", "", line).strip(" :-—\t")
            cleaned = _extract_document_number_candidate(same_line)
            if cleaned:
                return cleaned
            for candidate in lines[index + 1 : index + 8]:
                cleaned = _extract_document_number_candidate(candidate)
                if cleaned:
                    return cleaned

    patterns = [
        r"(?:накладн(?:ая|ой)?|счет|сч[её]т|invoice|inv)\D{0,25}([A-Za-zА-Яа-я0-9][A-Za-zА-Яа-я0-9\-/]{1,})",
        r"№\s*([A-Za-zА-Яа-я0-9][A-Za-zА-Яа-я0-9\-/]{1,})",
    ]
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if match:
            candidate = match.group(1)
            if any(ch.isdigit() for ch in candidate):
                return candidate
    return None


def _extract_document_number_candidate(value: str) -> str | None:
    lowered = value.lower().strip()
    if not lowered:
        return None
    if any(word in lowered for word in ("масса", "груза", "брутто", "нетто", "мест", "порядковых", "записей")):
        return None

    prefixed = _extract_prefixed_document_number(value)
    if prefixed:
        return prefixed

    if any(word in lowered for word in ("дата", "товарная", "накладная")):
        return None
    # Не принимаем дату за номер накладной.
    if re.fullmatch(r".*\b\d{1,2}[./-]\d{1,2}[./-]\d{2,4}\b.*", value):
        return None
    if re.fullmatch(r".*\b\d{1,2}\s+(?:января|февраля|марта|апреля|мая|июня|июля|августа|сентября|октября|ноября|декабря)\s+\d{4}\b.*", value, flags=re.IGNORECASE):
        return None

    match = re.search(r"\b([A-Za-zА-Яа-яЁ0-9]+(?:\s*[-/]\s*[A-Za-zА-Яа-яЁ0-9]+)+)\b", value)
    if match:
        return _normalize_document_number(match.group(1))
    match = re.fullmatch(r"\d{3,}", lowered)
    if match:
        return match.group(0)
    return None


def _extract_prefixed_document_number(value: str) -> str | None:
    for match in re.finditer(r"\b([A-Za-zА-Яа-яЁ]{1,8})(\s*[-–—]\s*)?(\d{2,})\b", value):
        prefix = match.group(1).strip()
        separator = match.group(2)
        digits = match.group(3)
        if prefix.lower() in {"no", "n", "от", "инн", "кпп"}:
            continue
        if prefix.isalpha() and any(ch.isdigit() for ch in digits):
            if separator:
                return f"{prefix}-{digits}"
            return f"{prefix}{digits}"
    return None


def _normalize_document_number(value: str | None) -> str | None:
    if not value:
        return None
    cleaned = _normalize_line(value).strip(" №NoN:;-—")
    prefixed = _extract_prefixed_document_number(cleaned)
    if prefixed:
        return prefixed
    return re.sub(r"\s*([-–—/])\s*", r"\1", cleaned) or None


def _extract_date(text: str) -> str | None:
    match = re.search(r"(\d{2})[./-](\d{2})[./-](\d{4})", text)
    if match:
        return f"{match.group(3)}-{match.group(2)}-{match.group(1)}"
    match = re.search(r"(\d{4})-(\d{2})-(\d{2})", text)
    if match:
        return match.group(0)
    match = re.search(r"\b(\d{1,2})[./-](\d{1,2})[./-](\d{2})\b", text)
    if match:
        day = int(match.group(1))
        month = int(match.group(2))
        year = 2000 + int(match.group(3))
        return f"{year}-{month:02d}-{day:02d}"

    months = {
        "января": "01",
        "янв": "01",
        "февраля": "02",
        "фев": "02",
        "марта": "03",
        "мар": "03",
        "апреля": "04",
        "апр": "04",
        "мая": "05",
        "май": "05",
        "июня": "06",
        "июн": "06",
        "июля": "07",
        "июл": "07",
        "августа": "08",
        "авг": "08",
        "сентября": "09",
        "сент": "09",
        "сен": "09",
        "октября": "10",
        "окт": "10",
        "ноября": "11",
        "ноя": "11",
        "декабря": "12",
        "дек": "12",
    }
    month_pattern = "|".join(sorted(months, key=len, reverse=True))
    match = re.search(rf"\b(\d{{1,2}})[\s.-]+({month_pattern})[\s.-]+(\d{{2,4}})\b", text, flags=re.IGNORECASE)
    if match:
        day = int(match.group(1))
        month = months[match.group(2).lower()]
        raw_year = int(match.group(3))
        year = raw_year if raw_year >= 100 else 2000 + raw_year
        return f"{year}-{month}-{day:02d}"
    return None


def _extract_items(lines: list[str]) -> list[dict]:
    receipt_items = _extract_cash_receipt_items(lines)
    if receipt_items:
        return receipt_items

    is_upd_document = _looks_like_upd_document(lines)
    upd_items = _extract_upd_items(lines)
    if upd_items:
        return upd_items

    # Для УПД нельзя безопасно запускать общие парсеры ТОРГ-12: Google OCR
    # часто читает номера колонок формы (1, 2, 3, 10а, ИНН) как товарные
    # строки. Если специальный УПД-парсер не смог уверенно собрать позиции,
    # лучше оставить товары на ручную проверку, чем заполнить таблицу мусором.
    if is_upd_document:
        return []

    items = []
    # Вариант 1: строка товара распознана одной строкой: название + количество + ед. изм. + цена + сумма.
    inline_pattern = re.compile(
        r"^(?P<name>.+?)\s+"
        r"(?P<qty>\d[\d\s]*(?:[,.]\d+)?)\s*"
        r"(?P<unit>шт|кг|л|г|мл|уп|кор|pcs|kg|l)?\s+"
        r"(?P<price>\d[\d\s]*(?:[,.]\d+)?)\s+"
        r"(?P<sum>\d[\d\s]*(?:[,.]\d+)?)$",
        flags=re.IGNORECASE,
    )
    for line in lines:
        normalized = re.sub(r"\s+", " ", line).strip()
        if re.match(r"^\d{1,3}\s+", normalized) and re.search(r"(?:^|\s)(шт|кг|л|г|мл|уп|кор|pcs|kg|l)\.?\b", normalized, flags=re.IGNORECASE):
            continue
        match = inline_pattern.match(normalized)
        if match:
            name = match.group("name").strip(" -—\t")
            if _looks_like_product_name(name):
                items.append(
                    _build_item(
                        name=name,
                        quantity=_to_float(match.group("qty")),
                        unit=match.group("unit") or "шт",
                        price=_to_float(match.group("price")),
                        line_sum=_to_float(match.group("sum")),
                        vat=None,
                        vat_sum=None,
                    )
                )

    # Вариант 2: реальный OCR ТОРГ-12 иногда даёт товарные строки отдельно,
    # а цены и суммы ниже по колонкам. Такой разбор надёжнее общих fallback-ов.
    mixed_column_items = _extract_mixed_column_torg12_items(lines)
    if mixed_column_items:
        return mixed_column_items

    # Вариант 3: товарное наименование иногда распознаётся отдельной строкой,
    # а все числовые значения идут ниже вертикальным блоком.
    vertical_items = _extract_vertical_torg12_items(lines)
    existing_names = {item["name"].lower() for item in items}
    for item in vertical_items:
        if item["name"].lower() not in existing_names:
            items.append(item)
            existing_names.add(item["name"].lower())

    # Вариант 3: Google Drive OCR часто читает таблицу ТОРГ-12 по ячейкам,
    # поэтому товар выглядит так:
    #   1 Пудра сахарная
    #   шт
    #   166
    #   1
    #   100,00
    #   20 000,00
    #   20
    #   4 000,00
    #   24 000,00
    # Старый парсер ожидал всё в одной строке и поэтому оставлял лист
    # «Товарные позиции» пустым.
    multiline_items = _extract_multiline_torg12_items(lines)
    existing_names = {item["name"].lower() for item in items}
    for item in multiline_items:
        if item["name"].lower() not in existing_names:
            items.append(item)
            existing_names.add(item["name"].lower())

    # Вариант 3: Google Drive OCR иногда читает ТОРГ-12 по колонкам:
    # сначала все наименования товаров, затем отдельно количество/цены/НДС/итоги.
    # В таком режиме старый parser видел только итоговую строку или технические коды.
    columnwise_items = _extract_columnwise_torg12_items(lines)
    if columnwise_items and (not items or len(columnwise_items) > len(items)):
        items = columnwise_items

    return items


UPD_ROW_STOP_WORDS = (
    "главный бухгалтер",
    "руководитель организации",
    "индивидуальный предприниматель",
    "документ составлен",
)

UPD_ITEM_BLOCK_STOP_WORDS = (
    "главный бухгалтер",
    "товар (груз) передал",
    "товар (груз) получил",
    "ответственный за правильность",
)


def _extract_upd_items(lines: list[str]) -> list[dict]:
    """Extract rows from Russian UPD / счет-фактура scans.

    Google Drive OCR often reads an UPD table in a mixed order: first the
    product row starts as "1 Еноки вес", then service column numbers follow,
    and only later the business values appear as "166 кг / 3,140 / 650,00 /
    2041,00 / Без НДС / 2041,00". Generic TORG-12 parsers treat those service
    column numbers as real quantity/price values, so UPD needs a stricter pass.
    """
    if not _looks_like_upd_document(lines):
        return []

    normalized_lines = [_normalize_line(line) for line in lines]
    table_start = _find_upd_items_table_start(normalized_lines)
    row_starts = _find_upd_row_starts(normalized_lines, table_start)
    items = []
    for position, row in enumerate(row_starts):
        start = row["source_index"] + 1
        next_row_index = row_starts[position + 1]["source_index"] if position + 1 < len(row_starts) else len(normalized_lines)
        end = min(next_row_index, _find_upd_item_block_end(normalized_lines, start))
        block = normalized_lines[start:end]
        item = _parse_upd_item_block(row["name"], block)
        if item is not None:
            items.append(item)
    return items


def _looks_like_upd_document(lines: list[str]) -> bool:
    joined = "\n".join(_normalize_line(line).lower() for line in lines[:160])
    return (
        "универсальный" in joined
        and "передаточный" in joined
        and ("счет-фактура" in joined or "счёт-фактура" in joined)
    )


def _find_upd_items_table_start(lines: list[str]) -> int:
    markers = (
        "код товара/ работ",
        "код товара",
        "наименование товара",
        "имущественного права",
    )
    for index, line in enumerate(lines):
        lowered = line.lower()
        if any(marker in lowered for marker in markers):
            return index + 1
    return _find_items_table_start(lines)


def _find_upd_row_starts(lines: list[str], start_index: int) -> list[dict]:
    rows = []
    seen_line_numbers = set()
    for index in range(start_index, len(lines)):
        line = lines[index]
        lowered = line.lower()
        if any(word in lowered for word in UPD_ROW_STOP_WORDS):
            break

        row_line = _strip_upd_product_row_hints(line)
        match = re.match(r"^(?P<line_no>\d{1,3})\s+(?P<name>[A-Za-zА-Яа-яЁё].{2,})$", row_line)
        if match:
            line_no = int(match.group("line_no"))
            if line_no in seen_line_numbers or not 1 <= line_no <= 200:
                continue
            name = _clean_product_name(match.group("name"))
            if not _is_probable_upd_product_name(name):
                continue
            rows.append({"line_number": line_no, "name": name, "source_index": index})
            seen_line_numbers.add(line_no)
            continue

        if _looks_like_header_or_total(line):
            continue

        if _is_integer_line(line) and index + 1 < len(lines):
            line_no = int(line)
            if line_no in seen_line_numbers or not 1 <= line_no <= 200:
                continue
            name = _clean_product_name(_strip_upd_product_row_hints(lines[index + 1]))
            if not _is_probable_upd_product_name(name):
                continue
            rows.append({"line_number": line_no, "name": name, "source_index": index + 1})
            seen_line_numbers.add(line_no)
    rows.sort(key=lambda item: item["source_index"])
    return rows


def _strip_upd_product_row_hints(value: str) -> str:
    cleaned = _normalize_line(value)
    cleaned = re.sub(r"(?i)\s+всего\s+к\s+оплате\s*\(.*?\)\s*$", "", cleaned).strip()
    return cleaned


def _is_probable_upd_product_name(value: str) -> bool:
    if not _is_probable_torg12_product_name(value):
        return False
    lowered = value.lower()
    forbidden = (
        "счет-фактура",
        "счёт-фактура",
        "передаточный документ",
        "документ",
        "статус",
        "ндс",
        "акциз",
        "от ",
        "единицу измерения",
        "единица измерения",
        "товаров",
        "работ",
        "услуг",
        "налог",
        "стоимость",
        "сумма",
        "код вида",
        "страна происхождения",
        "регистрационный номер",
    )
    if any(word in lowered for word in forbidden):
        return False
    if not re.search(r"[А-Яа-яЁё]{2,}", value):
        return False
    # В УПД рядом с товаром часто есть код вида НФ-00004122. Это не
    # наименование товара, хотя общий фильтр видит в нём буквы.
    if re.fullmatch(r"[A-ZА-ЯЁ]{1,5}[-–—]?\d{3,}", value.strip(), flags=re.IGNORECASE):
        return False
    return True


def _find_upd_item_block_end(lines: list[str], start_index: int) -> int:
    for index in range(start_index, len(lines)):
        lowered = lines[index].lower()
        if any(word in lowered for word in UPD_ITEM_BLOCK_STOP_WORDS):
            return index
    return len(lines)


def _parse_upd_item_block(name: str, block: list[str]) -> dict | None:
    unit_info = _find_upd_unit_line(block)
    if unit_info is None:
        return None
    unit_index, unit = unit_info
    after_unit = block[unit_index + 1 :]

    plain_numbers = []
    vat_percent = None
    no_vat = False
    for raw_line in after_unit[:40]:
        line = _normalize_line(raw_line)
        lowered = line.lower()
        if any(word in lowered for word in UPD_ITEM_BLOCK_STOP_WORDS):
            break
        if "без" in lowered and "ндс" in lowered:
            no_vat = True
            continue
        percent = _extract_percent_from_line(line)
        if percent is not None:
            vat_percent = percent
            continue
        if "акциз" in lowered:
            continue
        if _is_upd_service_or_column_hint(line):
            continue
        for value in _extract_numbers_from_text(line):
            if _looks_like_upd_noise_number(value, len(plain_numbers), line):
                continue
            plain_numbers.append(value)

    if len(plain_numbers) < 3:
        return None

    quantity = plain_numbers[0]
    price = plain_numbers[1]
    line_sum = plain_numbers[2]
    remaining_numbers = plain_numbers[3:]
    vat_sum = None
    total_with_vat = None

    if vat_percent is not None:
        vat_sum = _find_upd_amount_close_to(
            remaining_numbers,
            round(line_sum * vat_percent / 100, 2),
        )
        if vat_sum is not None:
            total_with_vat = _find_upd_amount_close_to(
                remaining_numbers,
                round(line_sum + vat_sum, 2),
                skip_value=vat_sum,
            )
    elif no_vat:
        vat_sum = None
        total_with_vat = _find_upd_amount_close_to(remaining_numbers, line_sum)

    if total_with_vat is None:
        total_with_vat = _find_upd_total_candidate(remaining_numbers, line_sum, vat_sum)

    if total_with_vat is not None and vat_sum is not None:
        line_sum = round(total_with_vat - vat_sum, 2)
    if vat_percent is None and total_with_vat is not None and abs(total_with_vat - line_sum) <= max(0.05, line_sum * 0.001):
        vat_sum = None

    if quantity <= 0 or price <= 0 or line_sum <= 0:
        return None
    expected = round(quantity * price, 2)
    if abs(expected - line_sum) > max(1.0, line_sum * 0.03):
        return None

    return _build_item(
        name=name,
        quantity=quantity,
        unit=unit,
        price=price,
        line_sum=line_sum,
        vat=vat_percent,
        vat_sum=vat_sum,
    )


def _extract_percent_from_line(line: str) -> float | None:
    match = re.search(r"(?<!\d)(\d{1,2}(?:[,.]\d+)?)\s*%", line)
    if not match:
        return None
    try:
        value = _to_float(match.group(1))
    except ValueError:
        return None
    return value if 0 <= value <= 100 else None


def _find_upd_amount_close_to(numbers: list[float], expected: float, skip_value: float | None = None) -> float | None:
    if expected <= 0:
        return None
    for value in numbers:
        if skip_value is not None and abs(value - skip_value) <= max(0.05, abs(skip_value) * 0.001):
            continue
        if abs(value - expected) <= max(0.05, abs(expected) * 0.01):
            return value
    return None


def _find_upd_total_candidate(numbers: list[float], line_sum: float, vat_sum: float | None) -> float | None:
    if vat_sum is not None:
        expected = round(line_sum + vat_sum, 2)
        found = _find_upd_amount_close_to(numbers, expected, skip_value=vat_sum)
        if found is not None:
            return found
    found = _find_upd_amount_close_to(numbers, line_sum)
    if found is not None:
        return found
    candidates = [value for value in numbers if value >= line_sum]
    return candidates[-1] if candidates else None

def _find_upd_unit_line(block: list[str]) -> tuple[int, str] | None:
    for index, line in enumerate(block[:120]):
        normalized = _normalize_line(line)
        match = re.search(r"(?:^|\s)(?:\d{3}\s+)?(?P<unit>шт|штука|штук|кг|кт|kt|л|г|мл|уп|кор|pcs|kg|l)\.?$", normalized, flags=re.IGNORECASE)
        if match:
            unit = match.group("unit").lower()
            if unit in {"штука", "штук", "pcs"}:
                unit = "шт"
            if unit in {"кт", "kt", "kg"}:
                unit = "кг"
            return index, unit
    return None


def _is_upd_service_or_column_hint(line: str) -> bool:
    lowered = line.lower().strip()
    if not lowered:
        return True
    if lowered in {"a", "x", "2a", "10а", "10a", "ба", "66"}:
        return True
    if re.fullmatch(r"\(?\d{1,2}[aа]?\)?", lowered):
        return True
    if re.fullmatch(r"\[\d{1,2}\]", lowered):
        return True
    service_words = (
        "код вида товара",
        "единица измерения",
        "условное обозна",
        "налоговая ставка",
        "сумма налога",
        "стоимость товаров",
        "страна происхождения",
        "регистрационный номер",
        "всего к оплате",
        "руководитель организации",
        "индивидуальный предприниматель",
        "основание передачи",
        "данные о транспортировке",
        "документ",
        "составлен на",
        "краткое наименова",
        "циф",
        "вой",
        "ние",
        "чение",
    )
    return any(word in lowered for word in service_words)


def _is_probable_upd_total_line(line: str, collected_numbers: list[float]) -> bool:
    if len(collected_numbers) < 3:
        return False
    values = _extract_numbers_from_text(line)
    if len(values) != 1:
        return False
    if not _is_number_line(line.strip(" -—")):
        return False
    return abs(values[0] - collected_numbers[2]) <= max(0.05, collected_numbers[2] * 0.001)


def _looks_like_upd_noise_number(value: float, collected_count: int, line: str | None = None) -> bool:
    # After the unit line the first useful three numbers are quantity, price,
    # and amount. OCR may still insert table column numbers 7/8/9/10. Values
    # 10/18/20 are kept when they are clearly printed as a VAT rate.
    lowered = (line or "").lower()
    if collected_count >= 3 and value in {0, 10, 18, 20} and ("%" in lowered or "ндс" in lowered or "став" in lowered):
        return False
    return collected_count >= 3 and value in {7, 8, 9, 10, 10.0, 11, 12, 13, 14, 15, 16, 17, 18, 19}


CASH_RECEIPT_STOP_WORDS = (
    "банковские оплаты",
    "безналичные оплаты",
    "наличные оплаты",
    "пло сбербанк",
    "терминал",
    "мерчант",
    "подпись клиента",
    "итого",
    "ккт",
    "фн",
    "фд",
    "фп",
    "рн ккт",
)


def _extract_cash_receipt_items(lines: list[str]) -> list[dict]:
    """Extract item blocks from Russian fiscal receipts.

    Google Drive OCR for long receipts usually returns blocks like:
        1 ТОВАР : ШТ. [M+]7362 КЕФИР ФЕРМЕРСКИЙ 800Г
        ЦЕНА : 72.90
        КОЛ-ВО: 1
        СУММА : 72.90
    This format is different from TORG-12, so it must be handled before the
    generic table parsers, otherwise header text and payment numbers can be
    incorrectly interpreted as one product row.
    """
    if not _looks_like_cash_receipt(lines):
        return []

    items = []
    index = 0
    while index < len(lines):
        normalized = _normalize_line(lines[index])
        name = _parse_cash_receipt_product_name(normalized)
        if not name:
            index += 1
            continue

        next_index = _find_next_cash_receipt_item_or_stop(lines, index + 1)
        # В реальном OCR цена/количество иногда попадают в ту же строку,
        # где начинается товар. Поэтому стартовую строку тоже добавляем
        # в блок для поиска меток ЦЕНА/КОЛ-ВО/СУММА.
        block = [normalized] + [_normalize_line(line) for line in lines[index + 1 : next_index]]
        item = _parse_cash_receipt_item_block(name, block)
        if item is not None:
            items.append(item)
        index = next_index if next_index > index else index + 1
    return items


def _looks_like_cash_receipt(lines: list[str]) -> bool:
    joined = "\n".join(_normalize_line(line).lower() for line in lines[:120])
    strong_markers = ("товарный чек", "кассир", "номер кассы", "смена:", "эклз", "фн:", "ккт")
    item_markers = ("кол-во", "кол во", "количество")
    has_strong_marker = any(marker in joined for marker in strong_markers)
    has_item_block = "товар" in joined and "цена" in joined and any(marker in joined for marker in item_markers)
    return has_strong_marker and has_item_block


def _parse_cash_receipt_product_name(line: str) -> str | None:
    normalized = _normalize_line(line)
    lowered = normalized.lower()
    if "товар" not in lowered:
        return None
    if any(marker in lowered for marker in ("товарный чек", "товарная накладная", "наименование товара")):
        return None

    value = re.sub(r"^\s*\d{1,3}\s+", "", normalized)
    value = re.sub(r"(?i)^.*?товар\s*[:;]?\s*", "", value, count=1)
    value = re.sub(r"(?i)^(?:шт|штука|штук)\.?\s*", "", value).strip()
    # В OCR кассового чека цена и количество могут оказаться в той же строке,
    # например: "... КЕФИР 800Г ЦЕНА: 72.90 кол-во: 1". Для названия берём
    # только часть до первой денежной метки.
    value = re.split(r"(?i)\b(?:цена|кол\s*-?\s*во|количество|сумма)\b\s*[:=]?", value, maxsplit=1)[0]
    value = _remove_cash_receipt_product_code(value)
    value = _clean_product_name(value)
    if not _looks_like_product_name(value):
        return None
    return value


def _remove_cash_receipt_product_code(value: str) -> str:
    cleaned = _normalize_line(value)
    # Google Drive OCR часто портит внутренний код товара: [M+]7362, [М+13649,
    # [N+13968, [+]8231. Код не нужен для iiko, поэтому убираем его даже если
    # закрывающая скобка потеряна.
    cleaned = re.sub(r"^\[[^\s\]]+\]?\s*", "", cleaned).strip()
    cleaned = re.sub(r"^\[[^\s\]]+\]?\s*", "", cleaned).strip()
    cleaned = re.sub(r"^\d{3,}\s*", "", cleaned).strip()
    return cleaned


def _find_next_cash_receipt_item_or_stop(lines: list[str], start_index: int) -> int:
    index = start_index
    while index < len(lines):
        current = _normalize_line(lines[index])
        lowered = current.lower()
        if _parse_cash_receipt_product_name(current):
            break
        if any(word in lowered for word in CASH_RECEIPT_STOP_WORDS):
            break
        index += 1
    return index


def _parse_cash_receipt_item_block(name: str, block: list[str]) -> dict | None:
    price = _find_cash_receipt_labeled_number(block, ("цена",))
    quantity = _find_cash_receipt_labeled_number(block, ("кол-во", "кол во", "количество"))
    line_sum = _find_cash_receipt_labeled_number(block, ("сумма",))

    if quantity is None and price is not None and line_sum is not None:
        quantity = round(line_sum / price, 3) if price else 1.0
    if quantity is None:
        quantity = 1.0
    if line_sum is None and price is not None:
        line_sum = round(price * quantity, 2)
    if price is None and line_sum is not None and quantity:
        price = round(line_sum / quantity, 2)
    if price is None or line_sum is None:
        return None
    if not _cash_receipt_amounts_are_consistent(quantity, price, line_sum):
        return None

    return _build_item(
        name=name,
        quantity=quantity,
        unit="шт",
        price=price,
        line_sum=line_sum,
        vat=None,
        vat_sum=None,
    )


def _find_cash_receipt_labeled_number(block: list[str], labels: tuple[str, ...]) -> float | None:
    for raw_line in block[:12]:
        line = _normalize_line(raw_line)
        value = _extract_number_after_any_label(line, labels)
        if value is not None:
            return value
    return None


def _extract_number_after_any_label(line: str, labels: tuple[str, ...]) -> float | None:
    for label in labels:
        label_pattern = _cash_receipt_label_pattern(label)
        match = re.search(
            rf"{label_pattern}\s*[:=]?\s*([^А-Яа-яЁёA-Za-z0-9]*)(\d[\d\s]*(?:[,.]\d+)?)",
            line,
            flags=re.IGNORECASE,
        )
        if not match:
            continue
        try:
            return _to_float(match.group(2))
        except ValueError:
            continue
    return None


def _cash_receipt_label_pattern(label: str) -> str:
    normalized = label.lower().strip()
    if normalized in {"кол-во", "кол во", "количество"}:
        return r"\b(?:кол\s*-?\s*во|количество)\b"
    return rf"\b{re.escape(normalized)}\b"


def _cash_receipt_amounts_are_consistent(quantity: float, price: float, line_sum: float) -> bool:
    if quantity <= 0 or price <= 0 or line_sum <= 0:
        return False
    expected = round(quantity * price, 2)
    return abs(expected - line_sum) <= max(0.05, line_sum * 0.03)


def _extract_vertical_torg12_items(lines: list[str]) -> list[dict]:
    items = []
    names = _extract_standalone_torg12_product_names(lines)
    if not names:
        return items
    for index, name_info in enumerate(names):
        start = name_info["source_index"] + 1
        end = names[index + 1]["source_index"] if index + 1 < len(names) else _find_items_table_end(lines, start)
        block = [_normalize_line(line) for line in lines[start:end]]
        parsed = _parse_vertical_item_block(name_info["name"], block)
        if parsed is not None:
            items.append(parsed)
    return items


def _extract_standalone_torg12_product_names(lines: list[str]) -> list[dict]:
    # Search only inside the goods table. Earlier versions scanned from the
    # beginning of the document and could treat supplier/receiver header lines
    # as product names on TORG-12 scans.
    start = _find_items_table_start(lines)
    end = _find_items_table_end(lines, start)
    names = []
    seen = set()
    for index in range(start, end):
        # If a product is already preceded by a separate line number, the
        # multiline parser handles it more accurately. This fallback is only
        # for scans where the item number is not attached to the name.
        if index > 0 and _is_integer_line(_normalize_line(lines[index - 1])):
            continue
        line = _clean_product_name(_normalize_line(lines[index]))
        if not _is_standalone_product_candidate(line):
            continue
        key = line.lower()
        if key in seen:
            continue
        names.append({"line_number": len(names) + 1, "name": line, "unit": "шт", "source_index": index})
        seen.add(key)
    return names


def _is_standalone_product_candidate(value: str) -> bool:
    if not _is_probable_torg12_product_name(value):
        return False
    if len(value.strip()) < 5:
        return False
    if not re.search(r"[А-Яа-яЁё]", value):
        return False
    if _is_number_line(value) or _looks_like_unit_or_ocr_unit(value):
        return False
    lowered = value.lower()
    # Avoid OCR fragments from table headers. Real standalone names in our
    # target documents contain product words or quotes.
    product_markers = ("«", "\"", "пиво", "коммутатор", "точка доступа", "сахар", "молоко", "сыр", "хлеб")
    return any(marker in lowered for marker in product_markers)


def _find_items_table_end(lines: list[str], start_index: int = 0) -> int:
    stop_words = (
        "итого",
        "всего по накладной",
        "товарная накладная имеет",
        "всего мест",
        "приложение",
        "масса груза",
        "по доверенности",
        "всего отпущено",
    )
    for index in range(start_index, len(lines)):
        lowered = _normalize_line(lines[index]).lower()
        if any(word in lowered for word in stop_words):
            return index
    return len(lines)


def _parse_vertical_item_block(name: str, block: list[str]) -> dict | None:
    unit = _find_unit(block) or "шт"
    numeric_lines = block
    unit_index = _find_unit_index(block)
    if unit_index is not None:
        numeric_lines = block[unit_index + 1 :]
    numbers = []
    for line in numeric_lines:
        numbers.extend(_extract_numbers_from_text(line))
    parsed_numbers = _parse_torg12_business_numbers(numbers, unit)
    if parsed_numbers is None:
        return None
    quantity, price, line_sum, vat, vat_sum = parsed_numbers
    return _build_item(name=name, quantity=quantity, unit=unit, price=price, line_sum=line_sum, vat=vat, vat_sum=vat_sum)


def _find_unit_index(block: list[str]) -> int | None:
    for index, line in enumerate(block[:120]):
        if _looks_like_unit_or_ocr_unit(line):
            return index
    return None


def _extract_multiline_torg12_items(lines: list[str]) -> list[dict]:
    items = []
    stop_words = (
        "итого",
        "всего по накладной",
        "товарная накладная имеет",
        "всего мест",
        "приложение",
        "масса груза",
        "по доверенности",
        "всего отпущено",
    )

    start_index = _find_items_table_start(lines)
    index = start_index
    while index < len(lines):
        normalized = _normalize_line(lines[index])
        normalized_lower = normalized.lower()
        if any(word in normalized_lower for word in stop_words):
            break

        inline_item = _parse_inline_torg12_row(normalized)
        if inline_item is not None:
            items.append(inline_item)
            index += 1
            continue

        start = _detect_multiline_item_start(lines, index)
        if start is None:
            index += 1
            continue

        _line_no, name, block_start = start
        block = []
        next_index = block_start
        while next_index < len(lines):
            current = _normalize_line(lines[next_index])
            current_lower = current.lower()
            if any(word in current_lower for word in stop_words):
                break
            if _parse_inline_torg12_row(current) is not None:
                break
            if _detect_multiline_item_start(lines, next_index) is not None:
                break
            block.append(current)
            next_index += 1

        parsed = _parse_multiline_item_block(name, block)
        if parsed is not None:
            items.append(parsed)
            index = next_index
        else:
            index += 1

    return items



def _items_look_suspicious(items: list[dict]) -> bool:
    if not items:
        return True
    for item in items:
        name = str(item.get("name") or "")
        if not _is_probable_torg12_product_name(name):
            return True
        quantity = item.get("quantity") or 0
        price = item.get("price") or 0
        line_sum = item.get("sum") or 0
        if quantity > 0 and price > 0 and line_sum > 0:
            expected = round(quantity * price, 2)
            if abs(expected - line_sum) > max(1.0, line_sum * 0.25):
                return True
    return False



def _extract_mixed_column_torg12_items(lines: list[str]) -> list[dict]:
    names = _extract_torg12_product_names(lines)
    if len(names) < 2:
        return []

    names = sorted(names, key=lambda item: item.get("source_index", 0))
    price_line_index, prices = _find_mixed_torg12_price_line(lines, names[-1]["source_index"] + 1, len(names))
    if price_line_index is None or len(prices) < len(names):
        return []

    row_parts = []
    for index, name_info in enumerate(names):
        start = name_info["source_index"] + 1
        end = names[index + 1]["source_index"] if index + 1 < len(names) else price_line_index
        block = [_normalize_line(line) for line in lines[start:end]]
        unit = _find_torg12_unit_near_row(block, lines)
        quantity = _extract_torg12_quantity_from_row_block(block)
        if quantity is None:
            return []
        row_parts.append({"name": name_info["name"], "unit": unit, "quantity": quantity})

    table_end = _find_items_table_end(lines, price_line_index + 1)
    money_candidates = _money_candidates_with_joined_thousands(lines, price_line_index + 1, table_end)
    items = []
    cursor = price_line_index
    for index, row in enumerate(row_parts):
        price = prices[index]
        expected_line_sum = round(row["quantity"] * price, 2)
        line_sum_candidate = _find_next_amount_close_to(money_candidates, expected_line_sum, cursor)
        if line_sum_candidate is None:
            return []
        vat_percent = _extract_percent_from_line(_normalize_line(lines[line_sum_candidate["line_index"]]))
        if vat_percent is None:
            vat_percent = _find_next_percent(lines, line_sum_candidate["line_index"], table_end)
        vat_sum = None
        total_with_vat = None
        if vat_percent is not None:
            expected_vat = round(line_sum_candidate["value"] * vat_percent / 100, 2)
            vat_sum_candidate = _find_next_amount_close_to(money_candidates, expected_vat, line_sum_candidate["line_index"])
            if vat_sum_candidate is not None:
                vat_sum = vat_sum_candidate["value"]
                expected_total = round(line_sum_candidate["value"] + vat_sum, 2)
                total_candidate = _find_next_amount_close_to(money_candidates, expected_total, vat_sum_candidate["line_index"])
                if total_candidate is not None:
                    total_with_vat = total_candidate["value"]
                    cursor = total_candidate["line_index"]
        if total_with_vat is None:
            cursor = line_sum_candidate["line_index"]

        items.append(
            _build_item(
                name=row["name"],
                quantity=row["quantity"],
                unit=row["unit"],
                price=price,
                line_sum=line_sum_candidate["value"],
                vat=vat_percent,
                vat_sum=vat_sum,
            )
        )

    return items


def _find_torg12_unit_near_row(block: list[str], all_lines: list[str]) -> str:
    unit = _find_unit(block)
    if unit:
        return unit
    for line in block[:5]:
        lowered = line.lower().strip(" .")
        if lowered in {"кт", "kt", "кг", "kg"}:
            return "кг"
    joined_header = "\n".join(_normalize_line(line).lower() for line in all_lines[:120])
    if ("масса" in joined_header or "macca" in joined_header) and any("166" in line for line in block[:6]):
        return "кг"
    return "шт"


def _extract_torg12_quantity_from_row_block(block: list[str]) -> float | None:
    candidates = []
    for line in block[:8]:
        lowered = line.lower().strip(" .")
        if lowered in {"кт", "kt", "кг", "kg"}:
            continue
        values = _extract_numbers_from_text(line)
        for value in values:
            if value <= 0:
                continue
            if int(value) == value and value >= 100:
                continue
            candidates.append(value)
    decimals = [value for value in candidates if abs(value - int(value)) > 1e-9]
    if decimals:
        return decimals[-1]
    return candidates[-1] if candidates else None


def _find_mixed_torg12_price_line(lines: list[str], start_index: int, count: int) -> tuple[int | None, list[float]]:
    for index in range(start_index, min(len(lines), start_index + 30)):
        line = _normalize_line(lines[index])
        lowered = line.lower()
        if any(marker in lowered for marker in ("итого", "всего по накладной")):
            break
        values = _extract_numbers_from_text(line)
        values = [value for value in values if value > 0]
        if len(values) >= count and not any("%" in token for token in re.findall(r"\S+", line)):
            if max(values) >= 50:
                return index, values[:count]
    return None, []


def _money_candidates_with_joined_thousands(lines: list[str], start_index: int, end_index: int) -> list[dict]:
    candidates = []
    index = start_index
    while index < min(end_index, len(lines)):
        line = _normalize_line(lines[index])
        values = _extract_numbers_from_text(line)
        for value in values:
            candidates.append({"value": value, "line_index": index, "raw": line})
        if index + 1 < min(end_index, len(lines)):
            next_line = _normalize_line(lines[index + 1])
            if re.fullmatch(r"\d{1,3}", line) and _is_number_line(next_line):
                prefix = int(line)
                next_values = _extract_numbers_from_text(next_line)
                if next_values and 0 < next_values[0] < 1000:
                    candidates.append({"value": prefix * 1000 + next_values[0], "line_index": index + 1, "raw": f"{line} {next_line}"})
        index += 1
    return candidates


def _find_next_amount_close_to(candidates: list[dict], expected: float, min_line_index: int) -> dict | None:
    if expected <= 0:
        return None
    matched = []
    for candidate in candidates:
        if candidate["line_index"] < min_line_index:
            continue
        value = candidate["value"]
        tolerance = max(0.05, abs(expected) * 0.015)
        if abs(value - expected) <= tolerance:
            matched.append((candidate["line_index"], abs(value - expected), candidate))
    if not matched:
        return None
    matched.sort(key=lambda item: (item[0], item[1]))
    return matched[0][2]


def _find_next_percent(lines: list[str], start_index: int, end_index: int) -> float | None:
    for index in range(start_index, min(end_index, start_index + 4, len(lines))):
        value = _extract_percent_from_line(_normalize_line(lines[index]))
        if value is not None:
            return value
    return None

def _extract_columnwise_torg12_items(lines: list[str]) -> list[dict]:
    names = _extract_torg12_product_names(lines)
    if not names:
        return []

    start_index = _find_numeric_columns_start(lines)
    tokens = _numeric_tokens(lines, start_index)
    if not tokens:
        return []

    row_groups = _extract_row_groups_from_numeric_columns(lines, start_index, len(names))
    if len(row_groups) >= len(names):
        return [
            _build_item(
                name=names[index]["name"],
                quantity=group["quantity"],
                unit=group.get("unit") or "шт",
                price=group["price"],
                line_sum=group["sum"],
                vat=group.get("vat_percent"),
                vat_sum=group.get("vat_sum"),
            )
            for index, group in enumerate(row_groups[: len(names)])
        ]

    totals = _select_amounts_with_vat(tokens, len(names))
    if not totals:
        return []

    quantities = _select_quantity_candidates(lines, start_index, len(names), totals[-1]["line_index"] if totals else None)
    if len(quantities) < len(names):
        quantities = _infer_missing_quantities_from_totals(totals, tokens, quantities, len(names))
    if len(quantities) < len(names):
        return []

    items = []
    for index, name_info in enumerate(names):
        total_token = totals[index]
        vat_sum = _find_vat_sum_before_total(tokens, total_token)
        total_with_vat = total_token["value"]
        line_sum = round(total_with_vat - vat_sum, 2) if vat_sum is not None else total_with_vat
        vat_percent = _infer_vat_percent(line_sum, vat_sum)
        quantity = quantities[index]
        price = round((line_sum / quantity) + 1e-9, 2) if quantity else 0
        items.append(
            _build_item(
                name=name_info["name"],
                quantity=quantity,
                unit=name_info.get("unit") or "шт",
                price=price,
                line_sum=line_sum,
                vat=vat_percent,
                vat_sum=vat_sum,
            )
        )
    return items


def _extract_torg12_product_names(lines: list[str]) -> list[dict]:
    names = []
    seen = set()
    for index, raw_line in enumerate(lines):
        line = _normalize_line(raw_line)
        if _looks_like_header_or_total(line):
            continue

        same_line_match = re.match(r"^(?P<line_no>\d{1,3})\s+(?P<name>[A-Za-zА-Яа-яЁё].{2,})$", line)
        if same_line_match:
            name = _clean_product_name(same_line_match.group("name"))
            line_no = int(same_line_match.group("line_no"))
            if _is_probable_torg12_product_name(name) and 1 <= line_no <= 200:
                key = name.lower()
                if key not in seen:
                    names.append({"line_number": line_no, "name": name, "unit": "шт", "source_index": index})
                    seen.add(key)
                continue

        if _is_integer_line(line) and index + 1 < len(lines):
            next_line = _normalize_line(lines[index + 1])
            if re.match(r"^\d{1,3}\s+", next_line):
                continue
            name = _clean_product_name(next_line)
            line_no = int(line)
            if _is_probable_torg12_product_name(name) and 1 <= line_no <= 200:
                key = name.lower()
                if key not in seen:
                    names.append({"line_number": line_no, "name": name, "unit": "шт", "source_index": index})
                    seen.add(key)

    names.sort(key=lambda item: (item["line_number"], item.get("source_index", 0)))
    contiguous = []
    expected_line = 1
    for item in names:
        if item["line_number"] == expected_line:
            contiguous.append(item)
            expected_line += 1
        elif item["line_number"] > expected_line and contiguous:
            break
    return contiguous or names


def _is_probable_torg12_product_name(value: str) -> bool:
    if not _looks_like_product_name(value):
        return False
    lowered = value.lower()
    if len(_extract_numbers_from_text(value)) > 2:
        return False
    forbidden = ("груз", "поставщик", "плательщик", "основание", "доверенности", "приложение")
    return not any(word in lowered for word in forbidden)


def _normalize_common_product_ocr(value: str) -> str:
    normalized = re.sub(r"\s+", " ", value).strip(" -—\t")
    lowered = normalized.lower()
    normalized = re.sub(r"(\d+)\s*[-–—]?\s*портов(?:ый|ой|ьй)?", r"\1-портовый", normalized, flags=re.IGNORECASE)
    normalized = re.sub(r"коммутатор\s+(\d+)\s+порт", r"коммутатор \1-порт", normalized, flags=re.IGNORECASE)
    normalized = re.sub(r"(?i)^гакет(?=\s*-?\s*майка)", "ПАКЕТ", normalized)
    normalized = re.sub(r"(?i)фермер\s+ский", "ФЕРМЕРСКИЙ", normalized)
    if "точка доступа" in lowered:
        if re.search(r"wi|fi|f1|6\)|6\]-?f|\bwi\b", lowered, flags=re.IGNORECASE):
            normalized = "Точка доступа Wi-Fi"
    return normalized


def _find_numeric_columns_start(lines: list[str]) -> int:
    markers = ("сумма с учетом", "сумма с учётом", "цена", "количество")
    for index, line in enumerate(lines):
        lowered = line.lower()
        if any(marker in lowered for marker in markers):
            return index + 1
    return _find_items_table_start(lines)


def _numeric_tokens(lines: list[str], start_index: int = 0) -> list[dict]:
    tokens = []
    for line_index, raw_line in enumerate(lines[start_index:], start=start_index):
        line = _normalize_line(raw_line).replace("'", "")
        if not line:
            continue
        if re.fullmatch(r"\d{3,6}\s+\d{2}", line):
            left, right = line.split()
            tokens.append({"value": float(f"{int(left)}.{right}"), "raw": line, "line_index": line_index, "is_percent": False})
            continue
        if re.fullmatch(r"[sS5]", line):
            tokens.append({"value": 5.0, "raw": line, "line_index": line_index, "is_percent": False, "ocr_digit": True})
            continue
        for match in _number_pattern().finditer(line):
            raw = match.group(0)
            try:
                tokens.append({"value": _to_float(raw), "raw": raw, "line_index": line_index, "is_percent": raw.endswith("%")})
            except ValueError:
                pass
    return tokens


def _extract_row_groups_from_numeric_columns(lines: list[str], start_index: int, expected_count: int) -> list[dict]:
    groups = []
    index = start_index
    while index < len(lines) and len(groups) < expected_count:
        line = _normalize_line(lines[index])
        if _looks_like_unit_or_ocr_unit(line) and index + 3 < len(lines):
            group = _parse_numeric_row_group(lines, index)
            if group is not None:
                groups.append(group)
                index = group["next_index"]
                continue
        index += 1
    return groups


def _looks_like_unit_or_ocr_unit(value: str) -> bool:
    lowered = value.lower().strip(" .")
    return lowered in {
        "шт",
        "шt",
        "iiit",
        "ш",
        "штука",
        "штук",
        "кг",
        "кт",
        "kt",
        "kg",
        "л",
        "l",
        "г",
        "мл",
        "уп",
        "кор",
        "-",
    }


def _normalize_unit(value: str | None) -> str:
    lowered = (value or "").lower().strip(" .")
    if lowered in {"штука", "штук", "pcs", "шt", "iiit", "ш"}:
        return "шт"
    if lowered in {"kg", "кт", "kt"}:
        return "кг"
    if lowered == "l":
        return "л"
    return lowered or "шт"


def _parse_numeric_row_group(lines: list[str], start_index: int) -> dict | None:
    unit = _normalize_unit(_normalize_line(lines[start_index]))
    cursor = start_index + 1
    if cursor >= len(lines):
        return None

    code_values = _extract_numbers_from_text(lines[cursor])
    if not code_values or not _looks_like_okei_code(code_values[0], unit):
        return None

    numbers = [code_values[0]]
    scan_cursor = cursor + 1
    while scan_cursor < len(lines) and len(numbers) < 10:
        current = _normalize_line(lines[scan_cursor])
        current_lower = current.lower()
        if _looks_like_unit_or_ocr_unit(current):
            break
        if any(marker in current_lower for marker in (
            "итого",
            "всего по накладной",
            "товарная накладная имеет",
            "всего мест",
            "приложение",
            "масса груза",
            "по доверенности",
            "всего отпущено",
        )):
            break
        if _looks_like_header_or_total(current) and "%" not in current:
            scan_cursor += 1
            continue
        numbers.extend(_extract_numbers_from_text(current))
        scan_cursor += 1

    parsed_numbers = _parse_torg12_business_numbers(numbers, unit)
    if parsed_numbers is None:
        return None

    quantity, price, line_sum, vat_percent, vat_sum = parsed_numbers
    if quantity <= 0 or price <= 0 or line_sum <= 0:
        return None

    expected = round(quantity * price, 2)
    if abs(expected - line_sum) > max(1.0, line_sum * 0.03):
        return None

    return {
        "quantity": quantity,
        "unit": unit,
        "price": price,
        "sum": line_sum,
        "vat_percent": vat_percent,
        "vat_sum": vat_sum,
        "next_index": scan_cursor,
    }


def _select_amounts_with_vat(tokens: list[dict], count: int) -> list[dict]:
    candidates = [token for token in tokens if not token.get("is_percent") and token["value"] >= 100]
    if len(candidates) < count:
        return []
    from itertools import combinations

    for total in reversed(candidates):
        total_value = total["value"]
        if total_value < 1000:
            continue
        previous = [token for token in candidates if token["line_index"] < total["line_index"] and 0 < token["value"] < total_value]
        previous = previous[-30:]
        for combo in combinations(previous, count):
            combo_sum = sum(token["value"] for token in combo)
            if abs(combo_sum - total_value) <= max(1.0, total_value * 0.001):
                return list(combo)
    # fallback: берём последние count денежных значений, но не общий итог.
    return candidates[-count:]


def _find_vat_sum_before_total(tokens: list[dict], total_token: dict) -> float | None:
    previous = [token for token in tokens if token["line_index"] < total_token["line_index"] and not token.get("is_percent")]
    candidates = []
    for token in reversed(previous[-10:]):
        value = token["value"]
        if 0 < value < total_token["value"] and 0.03 <= value / total_token["value"] <= 0.35:
            line_sum = total_token["value"] - value
            percent = value / line_sum * 100 if line_sum else 0
            standard_distance = min(abs(percent - standard) for standard in (10, 18, 20))
            candidates.append((standard_distance, -token["line_index"], value))
    if not candidates:
        return None
    candidates.sort()
    return candidates[0][2]


def _infer_vat_percent(line_sum: float, vat_sum: float | None) -> float | None:
    if not vat_sum or not line_sum:
        return None
    percent = vat_sum / line_sum * 100
    for standard in (0, 10, 18, 20):
        if abs(percent - standard) <= 0.6:
            return float(standard)
    return round(percent, 2)


def _select_quantity_candidates(lines: list[str], start_index: int, count: int, max_line_index: int | None = None) -> list[float]:
    values = []
    for current_index, line in enumerate(lines[start_index:], start=start_index):
        if max_line_index is not None and current_index >= max_line_index:
            break
        normalized = _normalize_line(line)
        if any(marker in normalized.lower() for marker in ("всего по", "beero", "доверенности", "листах")):
            break
        if re.fullmatch(r"[sS5]", normalized):
            values.append(5.0)
            continue
        if re.fullmatch(r"\d{1,2}", normalized):
            number = int(normalized)
            if 1 <= number <= max(9, count * 3):
                values.append(float(number))
    if len(values) >= count:
        return values[-count:]
    return values


def _infer_missing_quantities_from_totals(totals: list[dict], tokens: list[dict], quantities: list[float], count: int) -> list[float]:
    result = list(quantities)
    if len(result) >= count:
        return result[:count]
    return result

def _find_items_table_start(lines: list[str]) -> int:
    for candidate_index, line in enumerate(lines):
        lowered = line.lower()
        if "сумма с учетом" in lowered or "сумма с учётом" in lowered:
            return candidate_index + 1
    for candidate_index, line in enumerate(lines):
        lowered = line.lower()
        if "наименование" in lowered and "товар" in lowered:
            return candidate_index + 1
    return 0


def _parse_inline_torg12_row(line: str) -> dict | None:
    normalized = _normalize_line(line)
    match = re.match(r"^(?P<line_no>\d{1,3})\s+(?P<rest>.+)$", normalized)
    if not match:
        return None

    rest = match.group("rest").strip()
    if _looks_like_header_or_total(rest):
        return None

    unit_match = re.search(r"(?:^|\s)(шт|кг|л|г|мл|уп|кор|pcs|kg|l)\.?\b", rest, flags=re.IGNORECASE)
    if unit_match:
        name = rest[: unit_match.start()].strip(" -—	")
        unit = unit_match.group(1).lower()
        numeric_source = rest[unit_match.end() :]
    else:
        split_result = _split_name_and_numeric_tail(rest)
        if split_result is None:
            return None
        name, numeric_source = split_result
        unit = "шт"

    name = _clean_product_name(name)
    if not _looks_like_product_name(name):
        return None

    numbers = _extract_numbers_from_inline_tail(numeric_source)
    parsed_numbers = _parse_torg12_business_numbers(numbers, unit)
    if parsed_numbers is None:
        return None

    quantity, price, line_sum, vat, vat_sum = parsed_numbers
    return _build_item(name=name, quantity=quantity, unit=unit, price=price, line_sum=line_sum, vat=vat, vat_sum=vat_sum)


def _split_name_and_numeric_tail(rest: str) -> tuple[str, str] | None:
    matches = list(_number_pattern().finditer(rest))
    if len(matches) < 3:
        return None
    first_tail_match = matches[-6] if len(matches) >= 6 else matches[-3]
    name = rest[: first_tail_match.start()].strip(" -—	")
    numeric_source = rest[first_tail_match.start() :]
    if not name:
        return None
    return name, numeric_source


def _detect_multiline_item_start(lines: list[str], index: int) -> tuple[int, str, int] | None:
    current = _normalize_line(lines[index])
    if _looks_like_header_or_total(current):
        return None

    same_line_match = re.match(r"^(?P<line_no>\d{1,3})\s+(?P<name>[A-Za-zА-Яа-яЁё].{2,})$", current)
    if same_line_match:
        name = _clean_product_name(same_line_match.group("name"))
        line_no = int(same_line_match.group("line_no"))
        if 1 <= line_no <= 200 and _looks_like_product_name(name) and len(_extract_numbers_from_text(name)) <= 1:
            return line_no, name, index + 1

    if _is_integer_line(current) and index + 1 < len(lines):
        next_line = _normalize_line(lines[index + 1])
        if _looks_like_product_name(next_line) and not _looks_like_header_or_total(next_line):
            line_no = int(current)
            if 1 <= line_no <= 200:
                return line_no, _clean_product_name(next_line), index + 2

    return None


def _parse_multiline_item_block(name: str, block: list[str]) -> dict | None:
    unit = _find_unit(block) or "шт"
    numbers = []
    for line in block:
        numbers.extend(_extract_numbers_from_text(line))

    parsed_numbers = _parse_torg12_business_numbers(numbers, unit)
    if parsed_numbers is None:
        return None

    quantity, price, line_sum, vat, vat_sum = parsed_numbers
    return _build_item(
        name=name,
        quantity=quantity,
        unit=unit,
        price=price,
        line_sum=line_sum,
        vat=vat,
        vat_sum=vat_sum,
    )


def _parse_torg12_business_numbers(numbers: list[float], unit: str) -> tuple[float, float, float, float | None, float | None] | None:
    if not numbers:
        return None

    business_numbers = list(numbers)
    if business_numbers and _looks_like_okei_code(business_numbers[0], unit):
        business_numbers = business_numbers[1:]

    # Полная строка ТОРГ-12 обычно заканчивается так:
    # количество, цена, сумма без НДС, НДС %, НДС сумма, сумма с НДС.
    if len(business_numbers) >= 6 and 0 <= business_numbers[-3] <= 100:
        return (
            business_numbers[-6],
            business_numbers[-5],
            business_numbers[-4],
            business_numbers[-3],
            business_numbers[-2],
        )

    # Иногда OCR не возвращает последнюю колонку «Сумма с учетом НДС».
    if len(business_numbers) >= 5 and 0 <= business_numbers[-2] <= 100:
        return (
            business_numbers[-5],
            business_numbers[-4],
            business_numbers[-3],
            business_numbers[-2],
            business_numbers[-1],
        )

    if len(business_numbers) >= 3:
        return business_numbers[-3], business_numbers[-2], business_numbers[-1], None, None

    return None


def _number_pattern() -> re.Pattern:
    return re.compile(r"\d{1,3}(?:[\s ]\d{3})+(?:[,.]\d+)?%?|\d+(?:[,.]\d+)?%?")


def _extract_numbers_from_text(value: str) -> list[float]:
    result = []
    for match in _number_pattern().finditer(value.replace("X", " ").replace("x", " ")):
        try:
            result.append(_to_float(match.group(0)))
        except ValueError:
            pass
    return result


def _extract_numbers_from_inline_tail(value: str) -> list[float]:
    result = []
    normalized = value.replace("\u00a0", " ").replace("X", " ").replace("x", " ")
    for token in re.split(r"\s+", normalized):
        cleaned = token.strip(" -—\t")
        if not cleaned:
            continue
        if re.fullmatch(r"\d+(?:[,.]\d+)?%?", cleaned):
            try:
                result.append(_to_float(cleaned))
            except ValueError:
                pass
    return result


def _normalize_line(value: str) -> str:
    return re.sub(r"\s+", " ", value.replace(" ", " ")).strip()


def _clean_product_name(value: str) -> str:
    value = re.sub(r"\s+", " ", value).strip(" -—	")
    value = re.sub(r"^\d{1,3}\s+", "", value).strip()
    value = re.sub(r"\s+\d{3,6}(?:\s+\d{2,6})+$", "", value).strip()
    value = re.sub(r"\s+-\s*$", "", value).strip()
    return _normalize_common_product_ocr(value)


def _looks_like_header_or_total(value: str) -> bool:
    lowered = value.lower()
    return any(
        marker in lowered
        for marker in (
            "товарная накладная",
            "номер документа",
            "дата составления",
            "наименование",
            "характеристика",
            "артикул",
            "единица измерения",
            "количество",
            "сумма без",
            "сумма с учетом",
            "сумма с учётом",
            "итого",
            "всего по накладной",
            "код вида товара",
            "налоговая ставка",
            "сумма налога",
            "стоимость товаров",
            "страна происхождения",
            "регистрационный номер",
            "всего к оплате",
        )
    )


def _is_integer_line(value: str) -> bool:
    return bool(re.fullmatch(r"\d{1,3}", value.strip()))


def _build_item(
    name: str,
    quantity: float,
    unit: str,
    price: float,
    line_sum: float,
    vat: float | None,
    vat_sum: float | None,
) -> dict:
    return {
        "name": name,
        "quantity": quantity,
        "unit": unit or "шт",
        "price": price,
        "sum": line_sum,
        "vat": f"{vat:g}%" if vat is not None else None,
        "vat_percent": vat,
        "vat_sum": vat_sum,
        "comment": None,
        "confidence": None,
    }


def _looks_like_product_name(value: str) -> bool:
    lowered = value.lower().strip()
    if re.fullmatch(r"[\d\s,.]+(?:р|руб|руб\.)?", lowered):
        return False
    if re.fullmatch(r"[A-ZI1l|]{2,5}", value.strip()):
        return False
    forbidden = (
        "товара",
        "номер",
        "дата",
        "вид операции",
        "единица",
        "количество",
        "сумма",
        "ставка",
        "код",
        "форма",
        "года",
        "января",
        "февраля",
        "марта",
        "апреля",
        "мая",
        "июня",
        "июля",
        "августа",
        "сентября",
        "октября",
        "ноября",
        "декабря",
        "месте",
        "штук",
        "масса",
        "macca",
        "брутто",
        "нетто",
        "общее",
        "обще",
        "ответст",
        "должность",
    )
    forbidden_prefixes = ("от ", "«", "<<")
    return (
        len(value.strip()) >= 3
        and bool(re.search(r"[A-Za-zА-Яа-яЁё]", value))
        and not any(word in lowered for word in forbidden)
        and not lowered.startswith(forbidden_prefixes)
    )


def _find_unit(block: list[str]) -> str | None:
    unit_pattern = re.compile(r"^(шт|штука|штук|кг|кт|kt|л|г|мл|уп|кор|pcs|kg|l)\.?$", flags=re.IGNORECASE)
    for line in block[:50]:
        match = unit_pattern.match(line.strip())
        if match:
            unit = match.group(1).lower()
            if unit in {"штука", "штук"}:
                return "шт"
            if unit in {"кт", "kt", "kg"}:
                return "кг"
            return unit
    return None


def _is_number_line(value: str) -> bool:
    return bool(re.fullmatch(r"\d[\d\s]*(?:[,.]\d+)?%?", value.strip()))


def _looks_like_okei_code(value: float, unit: str) -> bool:
    if unit.lower() in {"шт", "pcs"}:
        return int(value) == value and value in {166, 796}
    return int(value) == value and 100 <= value <= 999


def _fallback_invoice_number(filename: str | None) -> str | None:
    if not filename:
        return None
    stem = Path(filename).stem
    return re.sub(r"[^A-Za-zА-Яа-я0-9_-]", "", stem)[:64] or None




def _normalize_organization_name(value: str) -> str:
    value = _normalize_line(value)
    value = re.sub(r"(?i)^(?:грузоотправитель|грузополучатель|поставщик|плательщик|продавец|покупатель)\s*[:\-—]?\s*", "", value).strip()
    original_value = value
    value = re.sub(r'(?<![А-Яа-яЁё])000(?=\s*["«])', 'ООО', value)
    value = re.sub(r"(?i)\bОбщество\s+с\s+ограниченной\s+ответственностью\b", "ООО", value).strip()
    if re.search(r"(?i)Общество\s+с\s+ограниченной\s+ответственностью", original_value):
        short_match = re.match(r'ООО\s*(["«][^"»]+["»])', value)
        if short_match:
            return f'ООО {short_match.group(1)}'
    value = _remove_torg12_service_hints(value)
    return value


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
    value = re.sub(pattern, "", value, flags=re.IGNORECASE)
    return re.sub(r"\s+", " ", value).strip(" ;,.-—")

def _clean_value(value: str) -> str | None:
    value = value.strip(" :-—\t")
    return value or None


def _to_float(value: str) -> float:
    cleaned = value.replace("\u00a0", " ").replace("'", "").replace(" ", "").replace("%", "").replace(",", ".")
    return float(cleaned)


def pretty_json(data: dict) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2)
