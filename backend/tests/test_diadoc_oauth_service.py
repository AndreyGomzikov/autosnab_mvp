from __future__ import annotations

from urllib.parse import parse_qs, urlparse

from app.config import settings
from app.services.diadoc_oauth_service import (
    build_diadoc_authorization_url,
    exchange_authorization_code,
    get_diadoc_access_token,
)


class FakeResponse:
    status_code = 200

    def json(self):
        return {
            "access_token": "new-access",
            "refresh_token": "new-refresh",
            "expires_in": 3600,
        }

    text = ""


def _configure(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "diadoc_client_id", "client-id")
    monkeypatch.setattr(settings, "diadoc_client_secret", "client-secret")
    monkeypatch.setattr(settings, "diadoc_access_token", None)
    monkeypatch.setattr(settings, "diadoc_refresh_token", "refresh-token")
    monkeypatch.setattr(settings, "diadoc_token_expiry", None)
    monkeypatch.setattr(settings, "secrets_env_file", str(tmp_path / ".env"))
    monkeypatch.setattr(settings, "diadoc_oauth_redirect_uri", "http://localhost/callback")


def test_authorization_code_flow_and_refresh(monkeypatch, tmp_path):
    _configure(monkeypatch, tmp_path)
    monkeypatch.setattr("app.services.diadoc_oauth_service.httpx.post", lambda *a, **k: FakeResponse())

    url = build_diadoc_authorization_url()
    query = parse_qs(urlparse(url).query)
    assert query["response_type"] == ["code"]
    assert "offline_access" in query["scope"][0]

    status = exchange_authorization_code("code", query["state"][0])
    assert status["has_refresh_token"] is True
    assert get_diadoc_access_token() == "new-access"
    env_text = (tmp_path / ".env").read_text()
    assert "DIADOC_REFRESH_TOKEN" in env_text
    assert "new-refresh" in env_text
