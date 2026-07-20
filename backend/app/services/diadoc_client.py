from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

import httpx

from app.config import settings
from app.services.diadoc_oauth_service import (
    DiadocOAuthAuthorizationError,
    DiadocOAuthConfigurationError,
    get_diadoc_access_token,
)

_RETRYABLE_STATUS_CODES = {408, 425, 429, 500, 502, 503, 504}


class DiadocApiError(RuntimeError):
    pass


class DiadocTransportError(DiadocApiError):
    pass


class DiadocContentTooLargeError(DiadocApiError):
    pass


@dataclass
class DiadocBinaryResponse:
    content: bytes
    content_type: str | None = None
    filename: str | None = None


class DiadocClient:
    def __init__(self) -> None:
        self.base_url = settings.diadoc_api_base_url.rstrip("/")
        self.timeout = settings.diadoc_timeout_seconds

    def get_json(
        self,
        path: str,
        *,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        response = self._request("GET", path, params=params)
        try:
            payload = response.json()
        except ValueError as exc:
            raise DiadocApiError("Диадок вернул невалидный JSON") from exc
        return payload if isinstance(payload, dict) else {"items": payload}

    def get_binary(
        self,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        accept: str = "application/octet-stream",
        max_bytes: int | None = None,
    ) -> DiadocBinaryResponse:
        response = self._request_binary(
            "GET",
            path,
            params=params,
            accept=accept,
            max_bytes=max_bytes,
        )
        return DiadocBinaryResponse(
            content=response.content,
            content_type=response.headers.get("Content-Type"),
            filename=_filename_from_content_disposition(
                response.headers.get("Content-Disposition")
            ),
        )

    def get_my_organizations(self) -> dict[str, Any]:
        return self.get_json(
            "/GetMyOrganizations",
            params={"autoRegister": "false"},
        )

    def get_box(self, *, box_id: str) -> dict[str, Any]:
        return self.get_json("/GetBox", params={"boxId": box_id})

    def get_document_types(self, *, box_id: str) -> dict[str, Any]:
        return self.get_json("/V3/GetDocumentTypes", params={"boxId": box_id})

    def get_last_event(self, *, box_id: str) -> dict[str, Any] | None:
        response = self._request_raw(
            "GET",
            "/V2/GetLastEvent",
            params={"boxId": box_id},
            accept="application/json; charset=utf-8",
        )
        if response.status_code == 204:
            return None
        self._raise_for_status(response)
        try:
            payload = response.json()
        except ValueError as exc:
            raise DiadocApiError("Диадок вернул невалидный JSON последнего события") from exc
        return payload if isinstance(payload, dict) else None

    def get_new_events(
        self,
        *,
        box_id: str,
        after_index_key: str | None,
        limit: int,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            "boxId": box_id,
            "messageType": "Letter",
            "documentDirection": "Inbound",
            "orderBy": "Ascending",
            "limit": max(1, min(limit, 500)),
        }
        if after_index_key:
            params["afterIndexKey"] = after_index_key
        if settings.diadoc_department_id:
            params["departmentId"] = settings.diadoc_department_id
        if settings.diadoc_type_named_ids:
            params["typeNamedId"] = settings.diadoc_type_named_ids
        return self.get_json("/V8/GetNewEvents", params=params)

    def get_message(self, *, box_id: str, message_id: str) -> dict[str, Any]:
        return self.get_json(
            "/V6/GetMessage",
            params={
                "boxId": box_id,
                "messageId": message_id,
                "injectEntityContent": "false",
            },
        )

    def get_entity_content(
        self,
        *,
        box_id: str,
        message_id: str,
        entity_id: str,
    ) -> DiadocBinaryResponse:
        return self.get_binary(
            "/V4/GetEntityContent",
            params={
                "boxId": box_id,
                "messageId": message_id,
                "entityId": entity_id,
            },
            max_bytes=max(1, settings.diadoc_max_attachment_bytes),
        )

    def generate_print_form(
        self,
        *,
        box_id: str,
        message_id: str,
        document_id: str,
    ) -> DiadocBinaryResponse:
        params = {
            "boxId": box_id,
            "messageId": message_id,
            "documentId": document_id,
        }
        attempts = max(1, settings.diadoc_print_form_attempts)
        last_error: Exception | None = None
        for attempt in range(attempts):
            try:
                response = self._request_binary(
                    "GET",
                    "/GeneratePrintForm",
                    params=params,
                    accept="application/pdf",
                    max_bytes=max(1, settings.diadoc_max_attachment_bytes),
                )
                retry_after = _retry_after_seconds(
                    response.headers.get("Retry-After")
                )
                if response.status_code == 200 and response.content:
                    return DiadocBinaryResponse(
                        content=response.content,
                        content_type=response.headers.get("Content-Type"),
                        filename=_filename_from_content_disposition(
                            response.headers.get("Content-Disposition")
                        ),
                    )
                if response.status_code == 200 and retry_after is not None:
                    if attempt + 1 < attempts:
                        time.sleep(retry_after)
                    continue
                self._raise_for_status(response)
                last_error = DiadocApiError(
                    "Диадок не вернул PDF печатной формы"
                )
            except DiadocApiError as exc:
                last_error = exc
                break
        if last_error is not None:
            raise last_error
        raise DiadocApiError("Не удалось получить PDF печатной формы Диадок")

    def _request_binary(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None,
        accept: str,
        max_bytes: int | None,
    ) -> httpx.Response:
        attempts = max(1, settings.diadoc_http_retry_attempts)
        last_transport_error: DiadocTransportError | None = None
        for attempt in range(attempts):
            try:
                response = self._send_streamed(
                    method,
                    path,
                    params=params,
                    accept=accept,
                    force_refresh=False,
                    max_bytes=max_bytes,
                )
                if response.status_code == 401 and settings.diadoc_refresh_token:
                    response = self._send_streamed(
                        method,
                        path,
                        params=params,
                        accept=accept,
                        force_refresh=True,
                        max_bytes=max_bytes,
                    )
            except DiadocTransportError as exc:
                last_transport_error = exc
                if attempt + 1 >= attempts:
                    raise
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
            self._raise_for_status(response)
            return response
        if last_transport_error is not None:
            raise last_transport_error
        raise DiadocApiError("Диадок API не вернул бинарное содержимое")

    def _send_streamed(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None,
        accept: str,
        force_refresh: bool,
        max_bytes: int | None,
    ) -> httpx.Response:
        try:
            token = get_diadoc_access_token(force_refresh=force_refresh)
        except (
            DiadocOAuthAuthorizationError,
            DiadocOAuthConfigurationError,
        ) as exc:
            raise DiadocApiError(str(exc)) from exc
        request = httpx.Request(method, f"{self.base_url}{path}", params=params)
        try:
            with httpx.Client(timeout=self.timeout) as client:
                with client.stream(
                    method,
                    f"{self.base_url}{path}",
                    headers={
                        "Authorization": f"Bearer {token}",
                        "Accept": accept,
                    },
                    params=params,
                ) as streamed:
                    chunks: list[bytes] = []
                    size = 0
                    body_limit = max_bytes if streamed.status_code < 400 else 65_536
                    for chunk in streamed.iter_bytes():
                        size += len(chunk)
                        if body_limit is not None and size > body_limit:
                            raise DiadocContentTooLargeError(
                                "Вложение Диадок превышает допустимый размер "
                                f"{body_limit} байт"
                            )
                        chunks.append(chunk)
                    return httpx.Response(
                        streamed.status_code,
                        headers=streamed.headers,
                        content=b"".join(chunks),
                        request=request,
                    )
        except DiadocContentTooLargeError:
            raise
        except httpx.HTTPError as exc:
            raise DiadocTransportError(
                f"Ошибка соединения с Диадок: {exc}"
            ) from exc

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        accept: str = "application/json; charset=utf-8",
    ) -> httpx.Response:
        response = self._request_raw(
            method,
            path,
            params=params,
            accept=accept,
        )
        self._raise_for_status(response)
        return response

    def _request_raw(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        accept: str,
    ) -> httpx.Response:
        attempts = max(1, settings.diadoc_http_retry_attempts)
        last_transport_error: DiadocTransportError | None = None
        for attempt in range(attempts):
            try:
                response = self._send(
                    method,
                    path,
                    params=params,
                    accept=accept,
                    force_refresh=False,
                )
                if response.status_code == 401 and settings.diadoc_refresh_token:
                    response = self._send(
                        method,
                        path,
                        params=params,
                        accept=accept,
                        force_refresh=True,
                    )
            except DiadocTransportError as exc:
                last_transport_error = exc
                if attempt + 1 >= attempts:
                    raise
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
            return response

        if last_transport_error is not None:
            raise last_transport_error
        raise DiadocApiError("Диадок API не вернул ответ")

    def _send(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None,
        accept: str,
        force_refresh: bool,
    ) -> httpx.Response:
        try:
            token = get_diadoc_access_token(force_refresh=force_refresh)
        except (
            DiadocOAuthAuthorizationError,
            DiadocOAuthConfigurationError,
        ) as exc:
            raise DiadocApiError(str(exc)) from exc
        try:
            with httpx.Client(timeout=self.timeout) as client:
                return client.request(
                    method,
                    f"{self.base_url}{path}",
                    headers={
                        "Authorization": f"Bearer {token}",
                        "Accept": accept,
                    },
                    params=params,
                )
        except httpx.HTTPError as exc:
            raise DiadocTransportError(
                f"Ошибка соединения с Диадок: {exc}"
            ) from exc

    @staticmethod
    def _raise_for_status(response: httpx.Response) -> None:
        if response.status_code < 400:
            return
        body = response.text[:500] if response.content else ""
        raise DiadocApiError(
            f"Диадок вернул HTTP {response.status_code}: {body}"
        )


def _filename_from_content_disposition(value: str | None) -> str | None:
    if not value:
        return None
    for part in value.split(";"):
        key, separator, raw = part.strip().partition("=")
        if separator and key.lower() in {"filename", "filename*"}:
            return raw.strip().strip('"').split("''")[-1]
    return None


def _retry_after_seconds(value: str | None) -> float | None:
    if not value:
        return None
    try:
        maximum = max(1.0, settings.diadoc_http_retry_max_delay_seconds)
        return max(0.0, min(float(value), maximum))
    except ValueError:
        return None


def _retry_delay(attempt: int, retry_after: str | None) -> float:
    explicit = _retry_after_seconds(retry_after)
    if explicit is not None:
        return explicit
    base = max(0.0, settings.diadoc_http_retry_base_delay_seconds)
    maximum = max(base, settings.diadoc_http_retry_max_delay_seconds)
    return min(base * (2 ** max(0, attempt)), maximum)
