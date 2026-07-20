import json

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.models.accounting import AccountingExport, AccountingMapping
from app.models.receiving import Receiving, ReceivingStatus
from app.schemas.accounting import AccountingExportResponse, AccountingMappingCreate, AccountingMappingResponse
from app.schemas.receiving import SendAccountingRequest, SendAccountingResponse
from app.services.receiving_service import build_accounting_payload

router = APIRouter(prefix="/accounting", tags=["accounting"])


@router.post("/mappings", response_model=AccountingMappingResponse)
def create_mapping(payload: AccountingMappingCreate, db: Session = Depends(get_db)):
    mapping = AccountingMapping(**payload.model_dump())
    db.add(mapping)
    db.commit()
    db.refresh(mapping)
    return mapping


@router.get("/mappings", response_model=list[AccountingMappingResponse])
def list_mappings(venue: str | None = None, db: Session = Depends(get_db)):
    query = db.query(AccountingMapping)
    if venue:
        query = query.filter(AccountingMapping.venue == venue)
    return query.order_by(AccountingMapping.id.desc()).all()


@router.post("/receivings/{receiving_id}/send", response_model=SendAccountingResponse)
def send_receiving_to_accounting(
    receiving_id: int, payload: SendAccountingRequest, db: Session = Depends(get_db)
):
    receiving = db.get(Receiving, receiving_id)
    if receiving is None:
        raise HTTPException(status_code=404, detail="Приемка не найдена")
    confirmed_statuses = {
        ReceivingStatus.confirmed_full,
        ReceivingStatus.confirmed_partial,
    }
    if receiving.status not in confirmed_statuses:
        raise HTTPException(
            status_code=400,
            detail="Передача в учетную систему возможна только после подтверждения приемки",
        )

    accounting_payload = build_accounting_payload(receiving)
    export_status = "prepared" if payload.dry_run else "sent_mock"
    export = AccountingExport(
        receiving_id=receiving.id,
        request_id=receiving.request_id,
        order_number=receiving.order_number,
        target_system=payload.target_system,
        status=export_status,
        payload_json=json.dumps(accounting_payload, ensure_ascii=False),
    )
    if not payload.dry_run:
        receiving.status = ReceivingStatus.sent_to_accounting
    if payload.comment:
        receiving.comment = payload.comment
    db.add(export)
    db.commit()
    db.refresh(export)
    return {
        "export_id": export.id,
        "receiving_id": receiving.id,
        "status": export.status,
        "target_system": export.target_system,
        "payload": accounting_payload,
    }


@router.get("/exports", response_model=list[AccountingExportResponse])
def list_exports(db: Session = Depends(get_db)):
    return db.query(AccountingExport).order_by(AccountingExport.id.desc()).all()
