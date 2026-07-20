from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class InvoiceDocumentData(BaseModel):
    model_config = ConfigDict(extra="forbid")

    document_date: str = ""
    document_number: str = ""
    supplier_name: str = ""
    supplier_inn: str = ""
    shipper: str = ""
    receiver: str = ""
    basis: str = ""
    document_form: str = ""
    total_without_vat: float | None = None
    vat_total: float | None = None
    total_with_vat: float | None = None


class InvoiceItemPackage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    value: float | None = None
    unit: str | None = None
    raw: str = ""


class InvoiceParsedItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    line_number: int | None = None
    raw_name: str = ""
    clean_name: str = ""
    normalized_name_candidate: str = ""
    brand_or_descriptor: str = ""
    package: InvoiceItemPackage = Field(default_factory=InvoiceItemPackage)
    document_unit: str = ""
    quantity_document: float | None = None
    units_per_package: float | None = None
    quantity_multiplier: float | None = None
    accounting_quantity_candidate: float | None = None
    accounting_unit_candidate: str = ""
    codes: list[str] = Field(default_factory=list)
    unit: str = ""
    quantity: float | None = None
    price: float | None = None
    amount_without_vat: float | None = None
    vat_rate: str = ""
    vat_amount: float | None = None
    amount_with_vat: float | None = None
    confidence: float | None = Field(default=None, ge=0, le=1)
    needs_review: bool = False
    review_reason: str = ""
    source_fragment: str = ""


class InvoiceReviewFlag(BaseModel):
    model_config = ConfigDict(extra="forbid")

    scope: Literal["document", "item"]
    line_number: int | None = None
    field: str = ""
    reason: str = ""
    severity: Literal["warning", "error"]


class InvoiceSourceTrace(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_type: Literal["pdf", "image", "excel", "xml", "unknown"] = "unknown"
    ocr_used: bool = False
    extraction_method: str = ""
    raw_text_sample: str = ""


class InvoiceParserResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    document: InvoiceDocumentData
    items: list[InvoiceParsedItem] = Field(default_factory=list)
    review_flags: list[InvoiceReviewFlag] = Field(default_factory=list)
    source_trace: InvoiceSourceTrace


class NormalizedInvoiceResult(InvoiceParserResult):
    upload_status: Literal["Проверить", "Требует проверки", "Не готово"] = "Проверить"
    row_status: Literal["Распознано", "Правка вручную", "Ошибка загрузки"] = "Распознано"
    duplicate: Literal["", "Да", "?"] = ""
    item_corrections: dict[int, str] = Field(default_factory=dict)
    normalization_log: list[str] = Field(default_factory=list)
