from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.schemas.receiving import (
    AccountingPayload,
    ApplyCorrectionsRequest,
    CompareInvoiceRequest,
    CompareInvoiceResponse,
    ConfirmReceivingRequest,
    CorrectionParseResponse,
    CorrectionTextRequest,
    DocumentResponse,
    DocumentUploadRequest,
    GoogleSheetsExportResponse,
    ReceivingResponse,
    StartReceivingRequest,
)
from app.services.correction_parser import parse_correction_text
from app.services.export_service import export_receiving_csv
from app.services.receiving_service import (
    add_document,
    apply_corrections,
    build_accounting_payload,
    compare_invoice,
    confirm_receiving,
    start_receiving,
)

router = APIRouter(prefix="/receiving", tags=["receiving"])


@router.post("/start", response_model=ReceivingResponse)
def start(payload: StartReceivingRequest, db: Session = Depends(get_db)):
    return start_receiving(db, payload)


@router.post("/{receiving_id}/documents", response_model=DocumentResponse)
def upload_document(receiving_id: int, payload: DocumentUploadRequest, db: Session = Depends(get_db)):
    try:
        return add_document(db, receiving_id, payload)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/{receiving_id}/compare-invoice", response_model=CompareInvoiceResponse)
def compare(receiving_id: int, payload: CompareInvoiceRequest, db: Session = Depends(get_db)):
    try:
        receiving = compare_invoice(db, receiving_id, payload)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return _compare_response(receiving)


@router.post("/{receiving_id}/corrections", response_model=CompareInvoiceResponse)
def corrections(receiving_id: int, payload: ApplyCorrectionsRequest, db: Session = Depends(get_db)):
    try:
        receiving = apply_corrections(db, receiving_id, payload)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return _compare_response(receiving)


@router.post("/{receiving_id}/corrections/parse", response_model=CorrectionParseResponse)
def parse_correction(payload: CorrectionTextRequest):
    parsed = parse_correction_text(payload.text)
    return {"intent": "apply_correction", "corrections": parsed.corrections}


@router.post("/{receiving_id}/corrections/text", response_model=CompareInvoiceResponse)
def corrections_from_text(receiving_id: int, payload: CorrectionTextRequest, db: Session = Depends(get_db)):
    parsed = parse_correction_text(payload.text)
    try:
        receiving = apply_corrections(db, receiving_id, parsed)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return _compare_response(receiving)


@router.post("/{receiving_id}/confirm", response_model=ReceivingResponse)
def confirm(receiving_id: int, payload: ConfirmReceivingRequest, db: Session = Depends(get_db)):
    try:
        return confirm_receiving(db, receiving_id, payload)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/{receiving_id}", response_model=ReceivingResponse)
def get_receiving(receiving_id: int, db: Session = Depends(get_db)):
    from app.models.receiving import Receiving

    receiving = db.get(Receiving, receiving_id)
    if receiving is None:
        raise HTTPException(status_code=404, detail="Приемка не найдена")
    return receiving


@router.get("/{receiving_id}/accounting-payload", response_model=AccountingPayload)
def accounting_payload(receiving_id: int, db: Session = Depends(get_db)):
    from app.models.receiving import Receiving

    receiving = db.get(Receiving, receiving_id)
    if receiving is None:
        raise HTTPException(status_code=404, detail="Приемка не найдена")
    return build_accounting_payload(receiving)


@router.post("/export/google-sheets-mvp", response_model=GoogleSheetsExportResponse)
def export_for_google_sheets(db: Session = Depends(get_db)):
    return export_receiving_csv(db)


def _compare_response(receiving):
    items = list(receiving.items)
    return {
        "receiving_id": receiving.id,
        "status": receiving.status.value,
        "total_order_items": len(receiving.order_items),
        "matched": sum(1 for item in items if item.status.value == "matched"),
        "missing": sum(1 for item in items if item.status.value == "missing"),
        "extra": sum(1 for item in items if item.status.value == "extra"),
        "quantity_mismatch": sum(1 for item in items if item.status.value == "quantity_mismatch"),
        "price_mismatch": sum(1 for item in items if item.status.value == "price_mismatch"),
        "manual_review": sum(1 for item in items if item.status.value in {"manual_review", "replacement_candidate", "crossed_out"}),
        "items": items,
    }
