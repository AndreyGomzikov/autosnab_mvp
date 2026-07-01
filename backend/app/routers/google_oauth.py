from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app.services.google_oauth_service import (
    GoogleOAuthAuthorizationError,
    GoogleOAuthConfigurationError,
    build_authorization_url,
    get_oauth_status,
    revoke_local_token,
    save_token_from_callback_url,
)

router = APIRouter(prefix="/google-oauth", tags=["google-oauth"])


@router.get("/status")
def google_oauth_status():
    return get_oauth_status()


@router.get("/authorize")
def google_oauth_authorize():
    try:
        return RedirectResponse(build_authorization_url())
    except GoogleOAuthConfigurationError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@router.get("/callback", response_class=HTMLResponse)
def google_oauth_callback(request: Request, error: str | None = None):
    if error:
        return HTMLResponse(
            f"""
            <h2>Google OAuth не выполнен</h2>
            <p>Ошибка Google: {error}</p>
            <p><a href="/api/v1/google-oauth/authorize">Попробовать снова</a></p>
            """,
            status_code=400,
        )
    try:
        status = save_token_from_callback_url(str(request.url))
    except (GoogleOAuthConfigurationError, GoogleOAuthAuthorizationError) as exc:
        return HTMLResponse(
            f"""
            <h2>Google OAuth не выполнен</h2>
            <p>{exc}</p>
            <p><a href="/api/v1/google-oauth/authorize">Попробовать снова</a></p>
            """,
            status_code=503,
        )
    return HTMLResponse(
        f"""
        <h2>Google OAuth подключен</h2>
        <p>Токен сохранён: {status.get('token_file')}</p>
        <p>Теперь можно вернуться на страницу загрузки накладной.</p>
        <p><a href="/api/v1/invoice-review/upload-page">Открыть загрузку накладной</a></p>
        """
    )


@router.post("/logout")
def google_oauth_logout():
    return revoke_local_token()
