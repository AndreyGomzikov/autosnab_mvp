from datetime import datetime
from types import SimpleNamespace

import pytest
from dotenv import dotenv_values

from app.services import google_oauth_service


def test_client_config_uses_settings(monkeypatch):
    monkeypatch.setattr(google_oauth_service.settings, "google_oauth_client_id", "client-id")
    monkeypatch.setattr(
        google_oauth_service.settings,
        "google_oauth_client_secret",
        "client-secret",
    )
    monkeypatch.setattr(
        google_oauth_service.settings,
        "google_oauth_redirect_uri",
        "http://localhost/callback",
    )

    config = google_oauth_service._client_config()

    assert config["web"]["client_id"] == "client-id"
    assert config["web"]["client_secret"] == "client-secret"
    assert config["web"]["redirect_uris"] == ["http://localhost/callback"]


def test_save_credentials_persists_env_values(monkeypatch):
    persisted = {}
    monkeypatch.setattr(google_oauth_service.settings, "google_oauth_access_token", None)
    monkeypatch.setattr(google_oauth_service.settings, "google_oauth_refresh_token", None)
    monkeypatch.setattr(google_oauth_service.settings, "google_oauth_token_expiry", None)
    monkeypatch.setattr(
        google_oauth_service,
        "_persist_env_values",
        lambda values: persisted.update(values),
    )
    credentials = SimpleNamespace(
        token="access-token",
        refresh_token="refresh-token",
        expiry=datetime(2026, 7, 4, 12, 30),
    )

    google_oauth_service._save_credentials(credentials)

    assert persisted == {
        "GOOGLE_OAUTH_ACCESS_TOKEN": "access-token",
        "GOOGLE_OAUTH_REFRESH_TOKEN": "refresh-token",
        "GOOGLE_OAUTH_TOKEN_EXPIRY": "2026-07-04T12:30:00",
    }


def test_parse_expiry_normalizes_utc_to_naive_datetime():
    assert google_oauth_service._parse_expiry("2026-07-04T12:30:00Z") == datetime(
        2026,
        7,
        4,
        12,
        30,
    )


def test_persist_env_values_creates_private_env_file(monkeypatch, tmp_path):
    env_file = tmp_path / ".env"
    monkeypatch.setattr(
        google_oauth_service.settings,
        "secrets_env_file",
        str(env_file),
    )

    google_oauth_service._persist_env_values({"EXAMPLE_SECRET": "value"})

    assert dotenv_values(env_file)["EXAMPLE_SECRET"] == "value"
    assert env_file.stat().st_mode & 0o777 == 0o600


def test_required_setting_rejects_missing_value():
    with pytest.raises(google_oauth_service.GoogleOAuthConfigurationError):
        google_oauth_service._required_setting(None, "GOOGLE_OAUTH_CLIENT_SECRET")
