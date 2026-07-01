from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from app.config import settings

GOOGLE_OAUTH_SCOPES = [
    "https://www.googleapis.com/auth/drive",
    "https://www.googleapis.com/auth/spreadsheets",
]


class GoogleOAuthConfigurationError(RuntimeError):
    pass


class GoogleOAuthAuthorizationError(RuntimeError):
    pass


def get_google_user_credentials():
    """Return OAuth user credentials for Google Drive and Google Sheets APIs.

    The token is stored locally in GOOGLE_OAUTH_TOKEN_FILE. If it is expired and
    contains a refresh token, the function refreshes it automatically.
    """
    try:
        from google.auth.exceptions import RefreshError
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
    except ImportError as exc:
        raise GoogleOAuthConfigurationError(
            "Не установлены зависимости Google OAuth. Выполните pip install -r backend/requirements.txt."
        ) from exc

    token_path = _token_path()
    if not token_path.exists():
        raise GoogleOAuthAuthorizationError(
            "Google OAuth не выполнен. Откройте /api/v1/google-oauth/authorize и войдите в Google-аккаунт."
        )

    credentials = Credentials.from_authorized_user_file(str(token_path), GOOGLE_OAUTH_SCOPES)
    if credentials.valid:
        return credentials

    if credentials.expired and credentials.refresh_token:
        try:
            credentials.refresh(Request())
        except RefreshError as exc:
            raise GoogleOAuthAuthorizationError(
                "Google OAuth token устарел или отозван. Откройте /api/v1/google-oauth/authorize и выполните вход заново."
            ) from exc
        _save_credentials(credentials)
        return credentials

    raise GoogleOAuthAuthorizationError(
        "Google OAuth token недействителен. Откройте /api/v1/google-oauth/authorize и выполните вход заново."
    )


def build_authorization_url() -> str:
    _allow_local_http_redirect()
    client_secret_path = _client_secrets_path()
    if not client_secret_path.exists():
        raise GoogleOAuthConfigurationError(
            f"Файл OAuth client secrets не найден: {client_secret_path}. "
            "Создайте OAuth Client ID в Google Cloud и сохраните JSON в backend/secrets/oauth-client.json."
        )

    try:
        from google_auth_oauthlib.flow import Flow
    except ImportError as exc:
        raise GoogleOAuthConfigurationError(
            "Не установлена зависимость google-auth-oauthlib. Выполните pip install -r backend/requirements.txt."
        ) from exc

    flow = Flow.from_client_secrets_file(
        str(client_secret_path),
        scopes=GOOGLE_OAUTH_SCOPES,
        redirect_uri=settings.google_oauth_redirect_uri,
    )
    authorization_url, _state = flow.authorization_url(
        access_type="offline",
        include_granted_scopes="true",
        prompt="consent",
    )
    return authorization_url


def save_token_from_callback_url(callback_url: str) -> dict[str, Any]:
    _allow_local_http_redirect()
    client_secret_path = _client_secrets_path()
    if not client_secret_path.exists():
        raise GoogleOAuthConfigurationError(
            f"Файл OAuth client secrets не найден: {client_secret_path}."
        )

    try:
        from google_auth_oauthlib.flow import Flow
    except ImportError as exc:
        raise GoogleOAuthConfigurationError(
            "Не установлена зависимость google-auth-oauthlib. Выполните pip install -r backend/requirements.txt."
        ) from exc

    flow = Flow.from_client_secrets_file(
        str(client_secret_path),
        scopes=GOOGLE_OAUTH_SCOPES,
        redirect_uri=settings.google_oauth_redirect_uri,
    )
    flow.fetch_token(authorization_response=callback_url)
    _save_credentials(flow.credentials)
    return get_oauth_status()


def get_oauth_status() -> dict[str, Any]:
    token_path = _token_path()
    client_secret_path = _client_secrets_path()
    result: dict[str, Any] = {
        "auth_mode": settings.google_auth_mode,
        "client_secrets_file": str(client_secret_path),
        "client_secrets_exists": client_secret_path.exists(),
        "token_file": str(token_path),
        "token_exists": token_path.exists(),
        "redirect_uri": settings.google_oauth_redirect_uri,
        "scopes": GOOGLE_OAUTH_SCOPES,
        "authorized": False,
    }
    if not token_path.exists():
        return result

    try:
        credentials = get_google_user_credentials()
        result["authorized"] = credentials.valid
        result["expired"] = credentials.expired
        result["has_refresh_token"] = bool(credentials.refresh_token)
        result["account"] = _extract_account_hint(token_path)
    except Exception as exc:  # noqa: BLE001 - status endpoint should show diagnostics
        result["error"] = str(exc)
    return result


def revoke_local_token() -> dict[str, Any]:
    token_path = _token_path()
    if token_path.exists():
        token_path.unlink()
    return get_oauth_status()


def _save_credentials(credentials) -> None:
    token_path = _token_path()
    token_path.parent.mkdir(parents=True, exist_ok=True)
    token_path.write_text(credentials.to_json(), encoding="utf-8")


def _extract_account_hint(token_path: Path) -> str | None:
    try:
        payload = json.loads(token_path.read_text(encoding="utf-8"))
    except Exception:
        return None
    return payload.get("account") or payload.get("client_id")


def _client_secrets_path() -> Path:
    return Path(settings.google_oauth_client_secrets_file)


def _token_path() -> Path:
    return Path(settings.google_oauth_token_file)


def _allow_local_http_redirect() -> None:
    redirect_uri = settings.google_oauth_redirect_uri.lower()
    if redirect_uri.startswith("http://localhost") or redirect_uri.startswith("http://127.0.0.1"):
        os.environ.setdefault("OAUTHLIB_INSECURE_TRANSPORT", "1")
