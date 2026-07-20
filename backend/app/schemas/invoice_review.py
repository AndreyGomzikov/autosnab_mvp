from pydantic import BaseModel, Field


class PipelineLogEntry(BaseModel):
    stage: str
    status: str
    message: str
    details: dict = Field(default_factory=dict)
    recommendation: str | None = None


class RecognizedInvoiceItem(BaseModel):
    name: str = Field(..., examples=["Молоко кокосовое Aroy-D 400 мл"])
    raw_name: str | None = None
    clean_name: str | None = None
    normalized_name_candidate: str | None = None
    brand_or_descriptor: str | None = None
    package: dict = Field(default_factory=dict)
    document_unit: str | None = None
    quantity_document: float | None = None
    units_per_package: float | None = None
    quantity_multiplier: float | None = None
    accounting_quantity_candidate: float | None = None
    accounting_unit_candidate: str | None = None
    codes: list[str] = Field(default_factory=list)
    needs_review: bool = False
    review_reason: str | None = None
    quantity: float = Field(..., examples=[5])
    unit: str = "шт"
    price: float = Field(..., examples=[250])
    sum: float | None = None
    vat: str | None = Field(default=None, examples=["20%"])
    comment: str | None = None
    confidence: float | None = Field(default=None, ge=0, le=1)

    # iiko fields required for real incomingInvoice import.
    line_number: int | None = Field(default=None, description="iiko item num")
    iiko_product_id: str | None = Field(default=None, description="iiko product GUID for <product>")
    product_article: str | None = Field(default=None, description="iiko product article for <productArticle>")
    supplier_product: str | None = Field(default=None, description="Supplier product GUID/name for <supplierProduct>")
    supplier_product_article: str | None = Field(default=None, description="Supplier article for <supplierProductArticle>")
    amount_unit: str | None = Field(default=None, description="iiko base unit for <amountUnit>")
    vat_percent: float | None = Field(default=None, description="VAT percent for <vatPercent>")
    vat_sum: float | None = Field(default=None, description="VAT amount for <vatSum>")
    store_id: str | None = Field(default=None, description="iiko store/account GUID for item <store>")
    mapping_status: str | None = Field(default=None, description="ready / needs_review for automatic iiko mapping")
    mapping_error: str | None = Field(default=None, description="Human-readable mapping problem, if any")
    correction: str | None = None
    amount_with_vat: float | None = None
    us_product_name: str | None = None
    product_code: str | None = None
    product_found: str | None = None
    us_unit: str | None = None
    quantity_us: float | None = None
    price_us: float | None = None
    conversion_factor: float | None = None
    conversion_method: str | None = None
    conversion_source_id: str | None = None
    conversion_review_reason: str | None = None
    package_reference_id: str | None = None


class InvoiceReviewCreateRequest(BaseModel):
    file_id: str | None = None
    file_type: str = "photo"
    file_url: str | None = None
    raw_text: str | None = None
    request_id: str | None = None
    supplier: str | None = None
    supplier_legal_name: str | None = None
    iiko_supplier_id: str | None = None
    invoice_date: str | None = None
    invoice_number: str | None = None
    document_number: str | None = None
    incoming_date: str | None = None
    due_date: str | None = None
    venue: str | None = None
    delivery_address: str | None = None
    display_store: str | None = None
    document_form: str | None = None
    supplier_inn: str | None = None
    shipper: str | None = None
    consignee: str | None = None
    recipient: str | None = None
    trade_point: str | None = None
    warehouse: str | None = None
    basis: str | None = None
    total_sum: float | None = None
    iiko_default_store_id: str | None = None
    iiko_organization: str | None = None
    iiko_organization_id: str | None = None
    chat_id: str | None = None
    user_id: str | None = None
    user_timezone: str | None = None
    user_utc_offset_minutes: int | None = None
    multipage_invoice: bool = False
    items: list[RecognizedInvoiceItem] = Field(default_factory=list)
    parser_metadata: dict = Field(default_factory=dict)


class InvoiceReviewUpdateRequest(BaseModel):
    raw_text: str | None = None
    supplier: str | None = None
    supplier_legal_name: str | None = None
    iiko_supplier_id: str | None = None
    invoice_date: str | None = None
    invoice_number: str | None = None
    document_number: str | None = None
    incoming_date: str | None = None
    due_date: str | None = None
    venue: str | None = None
    delivery_address: str | None = None
    display_store: str | None = None
    document_form: str | None = None
    supplier_inn: str | None = None
    shipper: str | None = None
    consignee: str | None = None
    recipient: str | None = None
    trade_point: str | None = None
    warehouse: str | None = None
    basis: str | None = None
    total_sum: float | None = None
    iiko_default_store_id: str | None = None
    iiko_organization: str | None = None
    iiko_organization_id: str | None = None
    user_timezone: str | None = None
    user_utc_offset_minutes: int | None = None
    multipage_invoice: bool = False
    items: list[RecognizedInvoiceItem]
    parser_metadata: dict = Field(default_factory=dict)


class ConfirmSendToIikoRequest(BaseModel):
    approved: bool = True
    dry_run: bool = False
    allow_with_warnings: bool = False
    target_organization: str | None = None
    target_organization_id: str | None = None
    target_warehouse: str | None = "Основной склад"
    target_warehouse_id: str | None = None
    approved_by: str | None = None
    comment: str | None = None


class SheetConfirmedItem(BaseModel):
    name: str
    quantity: float
    unit: str = "шт"
    price: float
    sum: float | None = None
    vat: str | None = None
    comment: str | None = None
    line_number: int | None = None
    iiko_product_id: str | None = None
    product_article: str | None = None
    supplier_product: str | None = None
    supplier_product_article: str | None = None
    amount_unit: str | None = None
    vat_percent: float | None = None
    vat_sum: float | None = None
    store_id: str | None = None
    mapping_status: str | None = None
    mapping_error: str | None = None


class SyncSheetAndConfirmRequest(ConfirmSendToIikoRequest):
    upload_status: str | None = None
    supplier: str | None = None
    supplier_legal_name: str | None = None
    iiko_supplier_id: str | None = None
    invoice_date: str | None = None
    invoice_number: str | None = None
    document_number: str | None = None
    incoming_date: str | None = None
    due_date: str | None = None
    venue: str | None = None
    delivery_address: str | None = None
    display_store: str | None = None
    document_form: str | None = None
    supplier_inn: str | None = None
    shipper: str | None = None
    consignee: str | None = None
    recipient: str | None = None
    trade_point: str | None = None
    warehouse: str | None = None
    basis: str | None = None
    total_sum: float | None = None
    iiko_default_store_id: str | None = None
    iiko_organization: str | None = None
    iiko_organization_id: str | None = None
    user_timezone: str | None = None
    user_utc_offset_minutes: int | None = None
    multipage_invoice: bool = False
    items: list[SheetConfirmedItem] = Field(default_factory=list)


class InvoiceReviewResponse(BaseModel):
    review_id: int
    status: str
    issues: list[str]
    spreadsheet_name: str
    csv_path: str | None = None
    google_spreadsheet_id: str | None = None
    google_spreadsheet_url: str | None = None
    google_spreadsheet_error: str | None = None
    ocr: dict | None = None
    parser_provider: str | None = None
    parser_notes: list[str] = Field(default_factory=list)
    pipeline_logs: list[PipelineLogEntry] = Field(default_factory=list)
    trace_metadata: dict = Field(default_factory=dict)
    next_actions: dict


class BotUploadAcceptedResponse(BaseModel):
    upload_id: str
    trace_id: str | None = None
    status: str
    message: str
    source_channel: str
    document_kind: str
    files_count: int
    review_id: int | None = None
    unsupported_reason: str | None = None


class BotDraftPageResponse(BaseModel):
    upload_id: str
    status: str
    message: str
    pages_count: int
    filenames: list[str] = Field(default_factory=list)
    unsupported_reason: str | None = None


class BotDraftInfo(BaseModel):
    upload_id: str
    pages_count: int
    filenames: list[str] = Field(default_factory=list)
    organization_name: str | None = None
    point_name: str | None = None


class BotDraftStatusResponse(BaseModel):
    draft: BotDraftInfo | None = None


class BotDraftResetResponse(BaseModel):
    status: str
    message: str


class BotDocumentSummary(BaseModel):
    supplier: str | None = None
    invoice_number: str | None = None
    invoice_date: str | None = None
    document_form: str | None = None
    total_sum: float | None = None
    items_count: int = 0
    pages_count: int = 0
    duplicate_indicator: str | None = None


class BotUploadStatusResponse(BaseModel):
    upload_id: str
    trace_id: str | None = None
    status: str
    message: str
    completed: bool
    source_channel: str
    document_kind: str
    files_count: int
    original_filename: str
    organization_name: str | None = None
    point_name: str | None = None
    user_id: str | None = None
    username: str | None = None
    review_id: int | None = None
    review_status: str | None = None
    result_code: str | None = None
    duplicate: bool = False
    error_text: str | None = None
    uploaded_at: str | None = None
    updated_at: str | None = None
    google_spreadsheet_url: str | None = None
    google_spreadsheet_error: str | None = None
    document_summary: BotDocumentSummary | None = None
    pipeline_logs: list[PipelineLogEntry] = Field(default_factory=list)
    next_actions: dict = Field(default_factory=dict)
