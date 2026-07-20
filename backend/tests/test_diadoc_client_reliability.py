from __future__ import annotations

import httpx

from app.config import settings
from app.services.diadoc_client import DiadocClient


def _response(status_code: int, body: bytes = b"{}") -> httpx.Response:
    return httpx.Response(
        status_code,
        content=body,
        request=httpx.Request(
            "GET",
            "https://diadoc-api.kontur.ru/test",
        ),
    )


def test_client_retries_transient_http_status(monkeypatch):
    client = DiadocClient()
    calls = {"count": 0}

    def fake_send(*_args, **_kwargs):
        calls["count"] += 1
        if calls["count"] == 1:
            return _response(503, b"temporary")
        return _response(200)

    monkeypatch.setattr(settings, "diadoc_http_retry_attempts", 3)
    monkeypatch.setattr(
        settings,
        "diadoc_http_retry_base_delay_seconds",
        0,
    )
    monkeypatch.setattr(client, "_send", fake_send)
    monkeypatch.setattr(
        "app.services.diadoc_client.time.sleep",
        lambda _seconds: None,
    )

    response = client._request_raw(
        "GET",
        "/test",
        accept="application/json",
    )

    assert response.status_code == 200
    assert calls["count"] == 2


def test_client_does_not_retry_forbidden(monkeypatch):
    client = DiadocClient()
    calls = {"count": 0}

    def fake_send(*_args, **_kwargs):
        calls["count"] += 1
        return _response(403, b"forbidden")

    monkeypatch.setattr(settings, "diadoc_http_retry_attempts", 4)
    monkeypatch.setattr(client, "_send", fake_send)

    response = client._request_raw(
        "GET",
        "/test",
        accept="application/json",
    )

    assert response.status_code == 403
    assert calls["count"] == 1


def test_streamed_binary_stops_when_size_limit_is_exceeded(monkeypatch):
    from app.services import diadoc_client as client_module
    from app.services.diadoc_client import DiadocContentTooLargeError

    class FakeStream:
        status_code = 200
        headers = {}

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def iter_bytes(self):
            yield b"1234"
            yield b"5678"

    class FakeHttpClient:
        def __init__(self, *_args, **_kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def stream(self, *_args, **_kwargs):
            return FakeStream()

    monkeypatch.setattr(client_module.httpx, "Client", FakeHttpClient)
    monkeypatch.setattr(client_module, "get_diadoc_access_token", lambda **_kwargs: "token")

    client = DiadocClient()
    with __import__("pytest").raises(DiadocContentTooLargeError):
        client._send_streamed(
            "GET",
            "/V4/GetEntityContent",
            params={},
            accept="application/octet-stream",
            force_refresh=False,
            max_bytes=5,
        )


def test_binary_request_retries_transport_error(monkeypatch):
    from app.services.diadoc_client import DiadocTransportError

    client = DiadocClient()
    calls = {"count": 0}

    def fake_streamed(*_args, **_kwargs):
        calls["count"] += 1
        if calls["count"] == 1:
            raise DiadocTransportError("temporary")
        return _response(200, b"file")

    monkeypatch.setattr(settings, "diadoc_http_retry_attempts", 2)
    monkeypatch.setattr(settings, "diadoc_http_retry_base_delay_seconds", 0)
    monkeypatch.setattr(client, "_send_streamed", fake_streamed)
    monkeypatch.setattr(
        "app.services.diadoc_client.time.sleep",
        lambda _seconds: None,
    )

    response = client._request_binary(
        "GET",
        "/V4/GetEntityContent",
        params={},
        accept="application/octet-stream",
        max_bytes=100,
    )

    assert response.content == b"file"
    assert calls["count"] == 2
