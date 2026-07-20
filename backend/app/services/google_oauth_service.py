from __future__ import annotations

import os
from datetime import datetime, timezone
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

    OAuth secrets are loaded from .env. Expired access tokens are refreshed and
    persisted back to the configured env file.
    """
    try:
        from google.auth.exceptions import RefreshError
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
    except ImportError as exc:
        raise GoogleOAuthConfigurationError(
            "Не установлены зависимости Google OAuth. Выполните pip install -r backend/requirements.txt."
        ) from exc

    if not settings.google_oauth_refresh_token:
        raise GoogleOAuthAuthorizationError(
            "Google OAuth не выполнен. Откройте /api/v1/google-oauth/authorize и войдите в Google-аккаунт."
        )

    credentials = Credentials(
        token=(
            settings.google_oauth_access_token
            if settings.google_oauth_token_expiry
            else None
        ),
        refresh_token=settings.google_oauth_refresh_token,
        token_uri=settings.google_oauth_token_uri,
        client_id=_required_setting(
            settings.google_oauth_client_id,
            "GOOGLE_OAUTH_CLIENT_ID",
        ),
        client_secret=_required_setting(
            settings.google_oauth_client_secret,
            "GOOGLE_OAUTH_CLIENT_SECRET",
        ),
        scopes=GOOGLE_OAUTH_SCOPES,
        expiry=_parse_expiry(settings.google_oauth_token_expiry),
    )
    if credentials.valid:
        return credentials

    if credentials.refresh_token:
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

    try:
        from google_auth_oauthlib.flow import Flow
    except ImportError as exc:
        raise GoogleOAuthConfigurationError(
            "Не установлена зависимость google-auth-oauthlib. Выполните pip install -r backend/requirements.txt."
        ) from exc

    flow = Flow.from_client_config(
        _client_config(),
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

    try:
        from google_auth_oauthlib.flow import Flow
    except ImportError as exc:
        raise GoogleOAuthConfigurationError(
            "Не установлена зависимость google-auth-oauthlib. Выполните pip install -r backend/requirements.txt."
        ) from exc

    flow = Flow.from_client_config(
        _client_config(),
        scopes=GOOGLE_OAUTH_SCOPES,
        redirect_uri=settings.google_oauth_redirect_uri,
    )
    flow.fetch_token(authorization_response=callback_url)
    _save_credentials(flow.credentials)
    return get_oauth_status()


def get_oauth_status() -> dict[str, Any]:
    result: dict[str, Any] = {
        "auth_mode": settings.google_auth_mode,
        "client_configured": bool(
            settings.google_oauth_client_id and settings.google_oauth_client_secret
        ),
        "token_configured": bool(settings.google_oauth_refresh_token),
        "redirect_uri": settings.google_oauth_redirect_uri,
        "scopes": GOOGLE_OAUTH_SCOPES,
        "authorized": False,
    }
    if not settings.google_oauth_refresh_token:
        return result

    try:
        credentials = get_google_user_credentials()
        result["authorized"] = credentials.valid
        result["expired"] = credentials.expired
        result["has_refresh_token"] = bool(credentials.refresh_token)
    except Exception as exc:  # noqa: BLE001 - status endpoint should show diagnostics
        result["error"] = str(exc)
    return result


def revoke_local_token() -> dict[str, Any]:
    _persist_env_values(
        {
            "GOOGLE_OAUTH_ACCESS_TOKEN": "",
            "GOOGLE_OAUTH_REFRESH_TOKEN": "",
            "GOOGLE_OAUTH_TOKEN_EXPIRY": "",
        }
    )
    settings.google_oauth_access_token = None
    settings.google_oauth_refresh_token = None
    settings.google_oauth_token_expiry = None
    return get_oauth_status()


def _save_credentials(credentials) -> None:
    expiry = credentials.expiry.isoformat() if credentials.expiry else ""
    values = {
        "GOOGLE_OAUTH_ACCESS_TOKEN": credentials.token or "",
        "GOOGLE_OAUTH_REFRESH_TOKEN": credentials.refresh_token or "",
        "GOOGLE_OAUTH_TOKEN_EXPIRY": expiry,
    }
    _persist_env_values(values)
    settings.google_oauth_access_token = credentials.token
    settings.google_oauth_refresh_token = credentials.refresh_token
    settings.google_oauth_token_expiry = expiry or None


def _persist_env_values(values: dict[str, str]) -> None:
    try:
        from dotenv import set_key
    except ImportError as exc:
        raise GoogleOAuthConfigurationError(
            "Не установлена зависимость python-dotenv. Выполните pip install -r backend/requirements.txt."
        ) from exc

    env_file = Path(settings.secrets_env_file)
    env_file.touch(mode=0o600, exist_ok=True)
    env_file.chmod(0o600)
    for key, value in values.items():
        set_key(str(env_file), key, value, quote_mode="always")


def _client_config() -> dict[str, dict[str, Any]]:
    return {
        "web": {
            "client_id": _required_setting(
                settings.google_oauth_client_id,
                "GOOGLE_OAUTH_CLIENT_ID",
            ),
            "client_secret": _required_setting(
                settings.google_oauth_client_secret,
                "GOOGLE_OAUTH_CLIENT_SECRET",
            ),
            "auth_uri": settings.google_oauth_auth_uri,
            "token_uri": settings.google_oauth_token_uri,
            "redirect_uris": [settings.google_oauth_redirect_uri],
        }
    }


def _required_setting(value: str | None, env_name: str) -> str:
    if not value:
        raise GoogleOAuthConfigurationError(
            f"Не задан {env_name}. Укажите его в .env."
        )
    return value


def _parse_expiry(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        expiry = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise GoogleOAuthConfigurationError(
            "GOOGLE_OAUTH_TOKEN_EXPIRY должен быть в ISO 8601 формате."
        ) from exc
    if expiry.tzinfo:
        return expiry.astimezone(timezone.utc).replace(tzinfo=None)
    return expiry


def _allow_local_http_redirect() -> None:
    redirect_uri = settings.google_oauth_redirect_uri.lower()
    if redirect_uri.startswith("http://localhost") or redirect_uri.startswith("http://127.0.0.1"):
        os.environ.setdefault("OAUTHLIB_INSECURE_TRANSPORT", "1")
