from __future__ import annotations

import hmac
from html import escape

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from app.config import settings
from app.db.session import get_db
from app.models.diadoc import DiadocDelivery, DiadocDocument
from app.schemas.diadoc import DiadocOrganization, DiadocStatusResponse, DiadocSyncResponse
from app.services.diadoc_client import DiadocApiError
from app.services.diadoc_oauth_service import (
    DiadocOAuthAuthorizationError,
    DiadocOAuthConfigurationError,
    build_diadoc_authorization_url,
    exchange_authorization_code,
    get_diadoc_oauth_status,
    revoke_local_diadoc_token,
)
from app.services.diadoc_scheduler_service import (
    diadoc_scheduler_status,
    start_diadoc_scheduler,
    stop_diadoc_scheduler,
)
from app.services.diadoc_sync_service import (
    get_diadoc_status,
    list_diadoc_organizations,
    retry_diadoc_deliveries,
    retry_failed_diadoc_documents,
    run_diadoc_preflight,
    sync_diadoc_documents,
)

router = APIRouter(prefix="/diadoc", tags=["diadoc"])


def require_diadoc_admin(
    request: Request,
    x_diadoc_api_key: str | None = Header(default=None, alias="X-Diadoc-Api-Key"),
    admin_key: str | None = Query(default=None),
) -> None:
    expected = settings.diadoc_admin_api_key or settings.bot_api_shared_secret
    if not expected:
        return
    supplied = x_diadoc_api_key or admin_key or ""
    if not hmac.compare_digest(supplied, expected):
        raise HTTPException(
            status_code=401,
            detail="Неверный или отсутствующий X-Diadoc-Api-Key.",
        )


@router.get("/status", response_model=DiadocStatusResponse)
def status(
    db: Session = Depends(get_db),
    _admin: None = Depends(require_diadoc_admin),
):
    return get_diadoc_status(db)


@router.get("/scheduler/status")
def scheduler_status(_admin: None = Depends(require_diadoc_admin)):
    return diadoc_scheduler_status()


@router.get("/preflight")
def preflight(_admin: None = Depends(require_diadoc_admin)):
    return run_diadoc_preflight()


@router.get("/organizations", response_model=list[DiadocOrganization])
def organizations(_admin: None = Depends(require_diadoc_admin)):
    try:
        return list_diadoc_organizations()
    except DiadocApiError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@router.post("/sync", response_model=DiadocSyncResponse)
def sync(
    create_google_sheet: bool = Query(default=True),
    db: Session = Depends(get_db),
    _admin: None = Depends(require_diadoc_admin),
):
    try:
        return sync_diadoc_documents(db, create_google_sheet=create_google_sheet)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except DiadocApiError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@router.post("/retry")
def retry_failed(
    create_google_sheet: bool = Query(default=True),
    db: Session = Depends(get_db),
    _admin: None = Depends(require_diadoc_admin),
):
    try:
        documents = retry_failed_diadoc_documents(
            db,
            create_google_sheet=create_google_sheet,
        )
        deliveries = retry_diadoc_deliveries(db)
        return {"documents": documents, "deliveries": deliveries}
    except DiadocApiError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@router.get("/dead-letter")
def dead_letter(
    db: Session = Depends(get_db),
    _admin: None = Depends(require_diadoc_admin),
):
    documents = (
        db.query(DiadocDocument)
        .filter(DiadocDocument.status == "dead_letter")
        .order_by(DiadocDocument.updated_at.desc())
        .limit(200)
        .all()
    )
    deliveries = (
        db.query(DiadocDelivery)
        .filter(DiadocDelivery.status == "dead_letter")
        .order_by(DiadocDelivery.updated_at.desc())
        .limit(200)
        .all()
    )
    return {
        "documents": [
            {
                "id": item.id,
                "message_id": item.message_id,
                "entity_id": item.entity_id,
                "filename": item.filename,
                "error": item.error_text,
                "updated_at": item.updated_at,
            }
            for item in documents
        ],
        "deliveries": [
            {
                "id": item.id,
                "document_id": item.diadoc_document_id,
                "delivery_type": item.delivery_type,
                "attempts": item.attempts,
                "error": item.last_error,
                "updated_at": item.updated_at,
            }
            for item in deliveries
        ],
    }


@router.post("/dead-letter/retry-all")
def retry_all_dead_letters(
    create_google_sheet: bool = Query(default=True),
    db: Session = Depends(get_db),
    _admin: None = Depends(require_diadoc_admin),
):
    documents = retry_failed_diadoc_documents(
        db,
        create_google_sheet=create_google_sheet,
        include_dead_letter=True,
        force=True,
    )
    deliveries = retry_diadoc_deliveries(
        db,
        include_dead_letter=True,
        force=True,
    )
    return {"documents": documents, "deliveries": deliveries}


@router.post("/dead-letter/{document_id}/retry")
def retry_dead_letter_document(
    document_id: int,
    create_google_sheet: bool = Query(default=True),
    db: Session = Depends(get_db),
    _admin: None = Depends(require_diadoc_admin),
):
    documents = retry_failed_diadoc_documents(
        db,
        create_google_sheet=create_google_sheet,
        include_dead_letter=True,
        force=True,
        document_id=document_id,
    )
    deliveries = retry_diadoc_deliveries(
        db,
        include_dead_letter=True,
        force=True,
        document_id=document_id,
    )
    return {"documents": documents, "deliveries": deliveries}


@router.get("/oauth/status")
def oauth_status(_admin: None = Depends(require_diadoc_admin)):
    return get_diadoc_oauth_status()


@router.get("/oauth/authorize")
def oauth_authorize(_admin: None = Depends(require_diadoc_admin)):
    try:
        return RedirectResponse(build_diadoc_authorization_url())
    except DiadocOAuthConfigurationError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@router.get("/oauth/callback", response_class=HTMLResponse)
def oauth_callback(
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
    error_description: str | None = None,
):
    if error:
        return HTMLResponse(
            _oauth_page(
                "Диадок OAuth не выполнен",
                error_description or error,
                success=False,
            ),
            status_code=400,
        )
    if not code or not state:
        return HTMLResponse(
            _oauth_page("Диадок OAuth не выполнен", "Не получены code или state.", success=False),
            status_code=400,
        )
    try:
        exchange_authorization_code(code, state)
        scheduler_started = start_diadoc_scheduler()
    except (DiadocOAuthConfigurationError, DiadocOAuthAuthorizationError) as exc:
        return HTMLResponse(
            _oauth_page("Диадок OAuth не выполнен", str(exc), success=False),
            status_code=503,
        )
    scheduler_message = (
        " Автоматическая синхронизация запущена."
        if scheduler_started
        else " Для запуска scheduler проверьте DIADOC_BOX_ID и настройки интеграции."
    )
    return HTMLResponse(
        _oauth_page(
            "Диадок OAuth подключен",
            "Access token, refresh token и срок действия сохранены." + scheduler_message,
            success=True,
        )
    )


@router.post("/oauth/logout")
def oauth_logout(_admin: None = Depends(require_diadoc_admin)):
    stop_diadoc_scheduler()
    return revoke_local_diadoc_token()


def _oauth_page(title: str, message: str, *, success: bool) -> str:
    retry = "" if success else '<p><a href="/api/v1/diadoc/oauth/authorize">Попробовать снова</a></p>'
    return f"""
    <!doctype html>
    <html lang="ru"><head><meta charset="utf-8"><title>{escape(title)}</title></head>
    <body>
      <h2>{escape(title)}</h2>
      <p>{escape(message)}</p>
      {retry}
      <p><a href="/api/v1/diadoc/status">Открыть статус интеграции</a></p>
      <script>
        if (window.opener) {{
          window.opener.postMessage({{ type: 'diadoc-oauth-{'success' if success else 'error'}' }}, window.location.origin);
          setTimeout(() => window.close(), 1200);
        }}
      </script>
    </body></html>
    """
