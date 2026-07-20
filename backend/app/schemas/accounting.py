from pydantic import BaseModel


class AccountingMappingCreate(BaseModel):
    venue: str
    supplier_product_name: str
    normalized_product_name: str
    accounting_product_id: str | None = None
    accounting_product_name: str | None = None
    unit: str | None = None
    status: str = "manual_confirmed"
    comment: str | None = None


class AccountingMappingResponse(AccountingMappingCreate):
    id: int

    class Config:
        from_attributes = True


class AccountingExportResponse(BaseModel):
    id: int
    receiving_id: int
    request_id: str
    order_number: str
    target_system: str
    status: str
    error_message: str | None = None

    class Config:
        from_attributes = True
