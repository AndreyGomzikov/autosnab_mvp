from __future__ import annotations

from xml.etree import ElementTree as ET

from app.schemas.invoice_review import InvoiceReviewCreateRequest, RecognizedInvoiceItem


def parse_diadoc_invoice_xml(content: bytes, *, file_id: str, file_url: str | None = None) -> InvoiceReviewCreateRequest:
    root = ET.fromstring(content)
    document_candidate = _first_descendant(root, {"Документ"})
    document = document_candidate if document_candidate is not None else root
    invoice_info = _first_descendant(document, {"СвСчФакт", "СвДокОбор"})
    supplier_node = _first_descendant(document, {"СвПрод", "ИдСв"})
    items_parent = _first_descendant(document, {"ТаблСчФакт", "ТаблДок"})

    invoice_number = _attr(invoice_info, "НомерСчФ", "НомДок", "НомерДок")
    invoice_date = _attr(invoice_info, "ДатаСчФ", "ДатаДок")
    supplier_source = supplier_node if supplier_node is not None else document
    supplier = _find_supplier_name(supplier_source)
    supplier_inn = _find_attr_recursive(supplier_source, "ИННЮЛ", "ИННФЛ", "ИНН")
    total_sum = _to_float(_find_attr_recursive(document, "СтТовУчНалВсего", "СумВсего", "ВсегоОпл"))
    basis = _find_basis(document)
    consignee = _find_party_name(document, {"СвПокуп", "ГрузПолуч"})
    shipper = _find_party_name(document, {"ГрузОт"})

    item_nodes = []
    if items_parent is not None:
        item_nodes = [node for node in items_parent.iter() if _local_name(node.tag) in {"СведТов", "СвТов"}]
    if not item_nodes:
        item_nodes = [node for node in document.iter() if _local_name(node.tag) in {"СведТов", "СвТов"}]

    items: list[RecognizedInvoiceItem] = []
    for index, node in enumerate(item_nodes, start=1):
        name = _attr(node, "НаимТов", "НаимТовПр", "Наименование") or f"Позиция {index}"
        quantity = _to_float(_attr(node, "КолТов", "Количество")) or 0.0
        price = _to_float(_attr(node, "ЦенаТов", "Цена")) or 0.0
        amount = _to_float(_attr(node, "СтТовУчНал", "Сумма", "СтТовБезНДС"))
        unit = _attr(node, "НаимЕдИзм", "ОКЕИ_Тов", "ЕдИзм") or "шт"
        vat = _attr(node, "НалСт", "СтавкаНДС")
        product_code = _attr(node, "КодТов", "АртикулТов", "Артикул")
        items.append(
            RecognizedInvoiceItem(
                name=name,
                raw_name=name,
                quantity=quantity,
                quantity_document=quantity,
                document_unit=unit,
                unit=unit,
                price=price,
                sum=amount,
                vat=vat,
                product_code=product_code,
                codes=[product_code] if product_code else [],
                confidence=1.0,
            )
        )

    return InvoiceReviewCreateRequest(
        file_id=file_id,
        file_type="xml",
        file_url=file_url,
        raw_text=_decode_xml_text(content),
        request_id=f"DIADOC-{file_id}",
        supplier=supplier,
        supplier_legal_name=supplier,
        supplier_inn=supplier_inn,
        shipper=shipper,
        consignee=consignee,
        recipient=consignee,
        basis=basis,
        invoice_number=invoice_number,
        document_number=invoice_number,
        invoice_date=invoice_date,
        incoming_date=invoice_date,
        document_form="УПД/ЭДО Диадок",
        total_sum=total_sum,
        items=items,
        parser_metadata={
            "provider": "diadoc_xml",
            "source_channel": "diadoc",
            "structured_source": True,
        },
    )


def _first_descendant(node: ET.Element, names: set[str]) -> ET.Element | None:
    for child in node.iter():
        if _local_name(child.tag) in names:
            return child
    return None


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _attr(node: ET.Element | None, *names: str) -> str | None:
    if node is None:
        return None
    for name in names:
        value = node.attrib.get(name)
        if value not in (None, ""):
            return value.strip()
    return None


def _find_attr_recursive(node: ET.Element, *names: str) -> str | None:
    for child in node.iter():
        value = _attr(child, *names)
        if value:
            return value
    return None


def _find_supplier_name(node: ET.Element) -> str | None:
    return _find_attr_recursive(node, "НаимОрг", "НаимОргПолн", "ФИО", "Наименование")


def _to_float(value: str | None) -> float | None:
    if not value:
        return None
    normalized = value.replace(" ", "").replace(",", ".")
    try:
        return float(normalized)
    except ValueError:
        return None


def _find_basis(node: ET.Element) -> str | None:
    basis_node = _first_descendant(node, {"ОснПер", "Основание", "СвОсн"})
    if basis_node is None:
        return _find_attr_recursive(node, "НаимОсн", "НомОсн", "Основание")
    name = _attr(basis_node, "НаимОсн", "НаимДокОсн", "Основание")
    number = _attr(basis_node, "НомОсн", "НомерОсн", "НомДокОсн")
    date = _attr(basis_node, "ДатаОсн", "ДатаДокОсн")
    parts = [part for part in (name, number, date) if part]
    return " ".join(parts) or None


def _find_party_name(node: ET.Element, names: set[str]) -> str | None:
    party = _first_descendant(node, names)
    if party is None:
        return None
    return _find_supplier_name(party)


def _decode_xml_text(content: bytes) -> str:
    declaration = content[:200].decode("ascii", errors="ignore")
    marker = 'encoding="'
    encoding = "utf-8"
    if marker in declaration:
        encoding = declaration.split(marker, 1)[1].split('"', 1)[0] or encoding
    try:
        return content.decode(encoding)
    except (LookupError, UnicodeDecodeError):
        return content.decode("utf-8", errors="replace")
