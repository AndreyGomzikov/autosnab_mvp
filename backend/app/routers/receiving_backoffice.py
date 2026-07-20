import json

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.models.accounting import AccountingExport
from app.models.receiving import Receiving, ReceivingDocument
from app.schemas.receiving import SendAccountingRequest
from app.services.receiving_backoffice_service import (
    build_discrepancy_analytics,
    build_iiko_payload,
    build_invoice_html,
    create_iiko_export,
    document_to_dict,
)

router = APIRouter(tags=["receiving-backoffice"])


@router.get("/receiving/{receiving_id}/documents")
def list_receiving_documents(receiving_id: int, db: Session = Depends(get_db)):
    receiving = db.get(Receiving, receiving_id)
    if receiving is None:
        raise HTTPException(status_code=404, detail="Приемка не найдена")
    return {
        "receiving_id": receiving.id,
        "order_number": receiving.order_number,
        "documents": [document_to_dict(document) for document in receiving.documents],
    }


@router.get("/documents/history")
def invoice_history(
    supplier: str | None = None,
    venue: str | None = None,
    db: Session = Depends(get_db),
):
    query = db.query(ReceivingDocument).join(Receiving)
    if supplier:
        query = query.filter(Receiving.supplier == supplier)
    if venue:
        query = query.filter(Receiving.venue == venue)
    documents = query.order_by(ReceivingDocument.created_at.desc()).all()
    return {"documents": [document_to_dict(document) for document in documents]}


@router.get("/documents/{document_id}")
def get_invoice_document(document_id: int, db: Session = Depends(get_db)):
    document = db.get(ReceivingDocument, document_id)
    if document is None:
        raise HTTPException(status_code=404, detail="Накладная не найдена")
    return document_to_dict(document)


@router.get("/documents/{document_id}/view", response_class=HTMLResponse)
def view_invoice_document(document_id: int, db: Session = Depends(get_db)):
    document = db.get(ReceivingDocument, document_id)
    if document is None:
        raise HTTPException(status_code=404, detail="Накладная не найдена")
    return HTMLResponse(build_invoice_html(document))


@router.get("/analytics/discrepancies")
def discrepancy_analytics(db: Session = Depends(get_db)):
    return build_discrepancy_analytics(db)


@router.get("/suppliers/control")
def supplier_control(db: Session = Depends(get_db)):
    analytics = build_discrepancy_analytics(db)
    return {
        "suppliers": analytics["by_supplier"],
        "rules": {
            "ok": "нет значимых расхождений",
            "watch": "есть повторяющиеся расхождения, нужен мониторинг",
            "control_required": "есть критичные расхождения или несовпадение поставщика",
        },
    }


@router.get("/iiko/receivings/{receiving_id}/payload")
def get_iiko_payload(receiving_id: int, db: Session = Depends(get_db)):
    receiving = db.get(Receiving, receiving_id)
    if receiving is None:
        raise HTTPException(status_code=404, detail="Приемка не найдена")
    return build_iiko_payload(receiving)


@router.post("/iiko/receivings/{receiving_id}/send")
def send_receiving_to_iiko(receiving_id: int, payload: SendAccountingRequest, db: Session = Depends(get_db)):
    receiving = db.get(Receiving, receiving_id)
    if receiving is None:
        raise HTTPException(status_code=404, detail="Приемка не найдена")
    try:
        export = create_iiko_export(db, receiving, payload.dry_run, payload.comment)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {
        "export_id": export.id,
        "receiving_id": receiving.id,
        "status": export.status,
        "target_system": export.target_system,
        "payload": json.loads(export.payload_json),
    }


@router.get("/iiko/exports")
def list_iiko_exports(db: Session = Depends(get_db)):
    exports = (
        db.query(AccountingExport)
        .filter(AccountingExport.target_system == "iiko")
        .order_by(AccountingExport.id.desc())
        .all()
    )
    return {
        "exports": [
            {
                "id": export.id,
                "receiving_id": export.receiving_id,
                "request_id": export.request_id,
                "order_number": export.order_number,
                "status": export.status,
                "created_at": export.created_at.isoformat() if export.created_at else None,
                "error_message": export.error_message,
            }
            for export in exports
        ]
    }
