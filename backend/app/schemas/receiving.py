from pydantic import BaseModel, Field


class OrderItemIn(BaseModel):
    name: str
    quantity: float
    unit: str = "шт"
    price: float = 0
    supplier_product_id: str | None = None
    role: str | None = None


class StartReceivingRequest(BaseModel):
    request_id: str = Field(..., examples=["REQ-000001"])
    order_number: str = Field(..., examples=["01TCPC4P-000001"])
    venue: str = Field(..., examples=["Добрая столовая"])
    supplier: str = Field(..., examples=["Питер Кельн"])
    delivery_address: str | None = None
    chat_id: str | None = None
    user_id: str | None = None
    order_items: list[OrderItemIn]


class ReceivingResponse(BaseModel):
    id: int
    request_id: str
    order_number: str
    venue: str
    supplier: str
    status: str
    comment: str | None = None

    class Config:
        from_attributes = True


class DocumentUploadRequest(BaseModel):
    file_id: str | None = None
    file_type: str = "photo"
    source: str = "max"
    file_url: str | None = None
    raw_text: str | None = None
    supplier_legal_name: str | None = None
    invoice_number: str | None = None
    invoice_date: str | None = None


class DocumentResponse(BaseModel):
    id: int
    receiving_id: int
    file_type: str
    source: str
    ocr_status: str
    invoice_number: str | None = None

    class Config:
        from_attributes = True


class InvoiceItemIn(BaseModel):
    name: str
    quantity: float
    unit: str = "шт"
    price: float = 0
    crossed_out: bool = False
    comment: str | None = None


class CompareInvoiceRequest(BaseModel):
    invoice_number: str | None = None
    invoice_date: str | None = None
    supplier_legal_name: str | None = None
    items: list[InvoiceItemIn]


class ReceivingItemResponse(BaseModel):
    id: int
    item_name_from_order: str | None
    item_name_from_invoice: str | None
    ordered_quantity: float
    received_quantity: float
    unit: str
    ordered_price: float
    invoice_price: float
    status: str
    comment: str | None = None

    class Config:
        from_attributes = True


class CompareInvoiceResponse(BaseModel):
    receiving_id: int
    status: str
    total_order_items: int
    matched: int
    missing: int
    extra: int
    quantity_mismatch: int
    price_mismatch: int
    manual_review: int
    items: list[ReceivingItemResponse]


class CorrectionIn(BaseModel):
    item_id: int | None = None
    item_query: str | None = None
    action: str = Field(..., examples=["mark_received", "reject", "set_quantity", "set_price"])
    quantity: float | None = None
    price: float | None = None
    comment: str | None = None


class ApplyCorrectionsRequest(BaseModel):
    corrections: list[CorrectionIn]


class ConfirmReceivingRequest(BaseModel):
    confirmed: bool = True
    partial: bool = False
    comment: str | None = None


class AccountingPayloadItem(BaseModel):
    name: str
    orderedQuantity: float
    receivedQuantity: float
    unit: str
    orderedPrice: float
    invoicePrice: float
    status: str
    comment: str | None = None


class AccountingPayload(BaseModel):
    requestId: str
    orderNumber: str
    venue: str
    supplier: dict
    invoice: dict
    items: list[AccountingPayloadItem]
    comment: str | None = None


class CorrectionTextRequest(BaseModel):
    text: str = Field(..., examples=["Сахара пришло 2 штуки, а не 1"])


class CorrectionParseResponse(BaseModel):
    intent: str = "apply_correction"
    corrections: list[CorrectionIn]


class SendAccountingRequest(BaseModel):
    target_system: str = "iiko"
    dry_run: bool = True
    comment: str | None = None


class SendAccountingResponse(BaseModel):
    export_id: int
    receiving_id: int
    status: str
    target_system: str
    payload: dict


class GoogleSheetsExportResponse(BaseModel):
    receiving_csv: str
    items_csv: str
    documents_csv: str
