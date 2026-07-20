from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import httpx

from app.config import settings

_TOKEN_LOCK = threading.Lock()
_STATE_LOCK = threading.Lock()
_USED_STATES: set[str] = set()
_RETRYABLE_STATUS_CODES = {408, 425, 429, 500, 502, 503, 504}


class DiadocOAuthConfigurationError(RuntimeError):
    pass


class DiadocOAuthAuthorizationError(RuntimeError):
    pass


def build_diadoc_authorization_url() -> str:
    client_id = _required(settings.diadoc_client_id, "DIADOC_CLIENT_ID")
    _required(settings.diadoc_client_secret, "DIADOC_CLIENT_SECRET")
    nonce = secrets.token_urlsafe(24)
    state = _build_state(nonce)
    params = {
        "response_type": "code",
        "client_id": client_id,
        "scope": settings.diadoc_oauth_scope,
        "redirect_uri": settings.diadoc_oauth_redirect_uri,
        "nonce": nonce,
        "state": state,
    }
    return (
        f"{settings.diadoc_oauth_authorize_uri}"
        f"?{urlencode(params)}"
    )


def exchange_authorization_code(
    code: str,
    state: str,
) -> dict[str, Any]:
    _validate_state(state)
    payload = _token_request(
        {
            "grant_type": "authorization_code",
            "code": code,
            "client_id": _required(
                settings.diadoc_client_id,
                "DIADOC_CLIENT_ID",
            ),
            "client_secret": _required(
                settings.diadoc_client_secret,
                "DIADOC_CLIENT_SECRET",
            ),
            "redirect_uri": settings.diadoc_oauth_redirect_uri,
        }
    )
    _save_token_payload(payload)
    return get_diadoc_oauth_status()


def get_diadoc_access_token(
    *,
    force_refresh: bool = False,
) -> str:
    token = (settings.diadoc_access_token or "").strip()
    should_refresh = (
        force_refresh
        or _token_near_expiry(settings.diadoc_token_expiry)
    )
    if token and not should_refresh:
        return token
    if not settings.diadoc_refresh_token:
        if token:
            return token
        raise DiadocOAuthAuthorizationError(
            "Диадок OAuth не выполнен. "
            "Откройте /api/v1/diadoc/oauth/authorize."
        )

    with _TOKEN_LOCK:
        token = (settings.diadoc_access_token or "").strip()
        if (
            token
            and not force_refresh
            and not _token_near_expiry(settings.diadoc_token_expiry)
        ):
            return token
        return _refresh_diadoc_access_token_unlocked()


def refresh_diadoc_access_token() -> str:
    with _TOKEN_LOCK:
        return _refresh_diadoc_access_token_unlocked()


def _refresh_diadoc_access_token_unlocked() -> str:
    refresh_token = _required(
        settings.diadoc_refresh_token,
        "DIADOC_REFRESH_TOKEN",
    )
    payload = _token_request(
        {
            "grant_type": "refresh_token",
            "client_id": _required(
                settings.diadoc_client_id,
                "DIADOC_CLIENT_ID",
            ),
            "client_secret": _required(
                settings.diadoc_client_secret,
                "DIADOC_CLIENT_SECRET",
            ),
            "refresh_token": refresh_token,
        }
    )
    _save_token_payload(payload)
    token = (settings.diadoc_access_token or "").strip()
    if not token:
        raise DiadocOAuthAuthorizationError(
            "OpenID Provider не вернул access_token."
        )
    return token


def get_diadoc_oauth_status() -> dict[str, Any]:
    client_configured = bool(
        settings.diadoc_client_id
        and settings.diadoc_client_secret
    )
    token_configured = bool(
        settings.diadoc_access_token
        or (
            settings.diadoc_refresh_token
            and client_configured
        )
    )
    return {
        "client_configured": client_configured,
        "token_configured": token_configured,
        "authorized": token_configured,
        "has_refresh_token": bool(settings.diadoc_refresh_token),
        "token_expiry": settings.diadoc_token_expiry,
        "redirect_uri": settings.diadoc_oauth_redirect_uri,
        "scope": settings.diadoc_oauth_scope,
    }


def revoke_local_diadoc_token() -> dict[str, Any]:
    _persist_env_values(
        {
            "DIADOC_ACCESS_TOKEN": "",
            "DIADOC_REFRESH_TOKEN": "",
            "DIADOC_TOKEN_EXPIRY": "",
        }
    )
    settings.diadoc_access_token = None
    settings.diadoc_refresh_token = None
    settings.diadoc_token_expiry = None
    return get_diadoc_oauth_status()


def _token_request(data: dict[str, str]) -> dict[str, Any]:
    attempts = max(1, settings.diadoc_http_retry_attempts)
    response: httpx.Response | None = None
    for attempt in range(attempts):
        try:
            response = httpx.post(
                settings.diadoc_oauth_token_uri,
                data=data,
                headers={"Accept": "application/json"},
                timeout=settings.diadoc_timeout_seconds,
            )
        except httpx.HTTPError as exc:
            if attempt + 1 >= attempts:
                raise DiadocOAuthAuthorizationError(
                    "Ошибка соединения с OpenID Provider Диадок: "
                    f"{exc}"
                ) from exc
            time.sleep(_retry_delay(attempt, None))
            continue

        if (
            response.status_code in _RETRYABLE_STATUS_CODES
            and attempt + 1 < attempts
        ):
            time.sleep(
                _retry_delay(
                    attempt,
                    response.headers.get("Retry-After"),
                )
            )
            continue
        break

    if response is None:
        raise DiadocOAuthAuthorizationError(
            "OpenID Provider Диадок не вернул ответ."
        )
    try:
        payload = response.json()
    except ValueError:
        payload = {
            "error_description": response.text[:500],
        }
    if response.status_code >= 400:
        detail = (
            payload.get("error_description")
            or payload.get("error")
            or response.text[:500]
        )
        raise DiadocOAuthAuthorizationError(
            "Диадок OAuth вернул HTTP "
            f"{response.status_code}: {detail}"
        )
    if (
        not isinstance(payload, dict)
        or not payload.get("access_token")
    ):
        raise DiadocOAuthAuthorizationError(
            "OpenID Provider Диадок вернул невалидный ответ."
        )
    return payload


def _save_token_payload(payload: dict[str, Any]) -> None:
    expires_in = int(payload.get("expires_in") or 0)
    expiry = ""
    if expires_in > 0:
        expiry = (
            datetime.now(timezone.utc)
            + timedelta(seconds=expires_in)
        ).isoformat()
    refresh_token = str(
        payload.get("refresh_token")
        or settings.diadoc_refresh_token
        or ""
    )
    values = {
        "DIADOC_ACCESS_TOKEN": str(
            payload.get("access_token") or ""
        ),
        "DIADOC_REFRESH_TOKEN": refresh_token,
        "DIADOC_TOKEN_EXPIRY": expiry,
    }
    _persist_env_values(values)
    settings.diadoc_access_token = (
        values["DIADOC_ACCESS_TOKEN"] or None
    )
    settings.diadoc_refresh_token = refresh_token or None
    settings.diadoc_token_expiry = expiry or None


def _persist_env_values(values: dict[str, str]) -> None:
    try:
        from dotenv import set_key
    except ImportError as exc:
        raise DiadocOAuthConfigurationError(
            "Не установлена зависимость python-dotenv."
        ) from exc

    env_file = Path(settings.secrets_env_file)
    try:
        env_file.parent.mkdir(parents=True, exist_ok=True)
        env_file.touch(mode=0o600, exist_ok=True)
        try:
            env_file.chmod(0o600)
        except OSError:
            pass
        for key, value in values.items():
            set_key(
                str(env_file),
                key,
                value,
                quote_mode="always",
            )
    except OSError as exc:
        raise DiadocOAuthConfigurationError(
            "Не удалось сохранить токены Диадок в "
            f"{env_file}: {exc}"
        ) from exc


def _token_near_expiry(value: str | None) -> bool:
    if not value:
        return False
    try:
        expiry = datetime.fromisoformat(
            value.replace("Z", "+00:00")
        )
    except ValueError:
        return True
    if expiry.tzinfo is None:
        expiry = expiry.replace(tzinfo=timezone.utc)
    return (
        expiry
        <= datetime.now(timezone.utc)
        + timedelta(minutes=5)
    )


def _build_state(nonce: str) -> str:
    payload = {
        "nonce": nonce,
        "iat": int(time.time()),
    }
    encoded = _b64url(
        json.dumps(
            payload,
            separators=(",", ":"),
        ).encode("utf-8")
    )
    signature = hmac.new(
        _state_secret(),
        encoded.encode("ascii"),
        hashlib.sha256,
    ).digest()
    return f"{encoded}.{_b64url(signature)}"


def _validate_state(state: str) -> None:
    try:
        encoded, supplied_signature = state.split(".", 1)
        expected = hmac.new(
            _state_secret(),
            encoded.encode("ascii"),
            hashlib.sha256,
        ).digest()
        if not hmac.compare_digest(
            _b64url(expected),
            supplied_signature,
        ):
            raise ValueError("signature")
        payload = json.loads(_b64url_decode(encoded))
        issued_at = int(payload["iat"])
        nonce = str(payload["nonce"])
        if not nonce:
            raise ValueError("nonce")
    except (
        ValueError,
        KeyError,
        TypeError,
        json.JSONDecodeError,
    ) as exc:
        raise DiadocOAuthAuthorizationError(
            "Некорректный OAuth state Диадок."
        ) from exc

    if abs(int(time.time()) - issued_at) > 600:
        raise DiadocOAuthAuthorizationError(
            "OAuth state Диадок истек. "
            "Начните авторизацию заново."
        )

    state_digest = hashlib.sha256(
        state.encode("utf-8")
    ).hexdigest()
    with _STATE_LOCK:
        if state_digest in _USED_STATES:
            raise DiadocOAuthAuthorizationError(
                "OAuth state Диадок уже был использован."
            )
        _USED_STATES.add(state_digest)
        if len(_USED_STATES) > 1000:
            _USED_STATES.clear()
            _USED_STATES.add(state_digest)


def _state_secret() -> bytes:
    return _required(
        settings.diadoc_client_secret,
        "DIADOC_CLIENT_SECRET",
    ).encode("utf-8")


def _b64url(value: bytes) -> str:
    return (
        base64.urlsafe_b64encode(value)
        .decode("ascii")
        .rstrip("=")
    )


def _b64url_decode(value: str) -> str:
    padded = value + "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(
        padded
    ).decode("utf-8")


def _retry_delay(
    attempt: int,
    retry_after: str | None,
) -> float:
    maximum = max(
        1.0,
        settings.diadoc_http_retry_max_delay_seconds,
    )
    if retry_after:
        try:
            return max(
                0.0,
                min(float(retry_after), maximum),
            )
        except ValueError:
            pass
    base = max(
        0.0,
        settings.diadoc_http_retry_base_delay_seconds,
    )
    return min(
        base * (2 ** max(0, attempt)),
        maximum,
    )


def _required(
    value: str | None,
    name: str,
) -> str:
    if not value:
        raise DiadocOAuthConfigurationError(
            f"Не задан {name}. Укажите его в .env."
        )
    return value
