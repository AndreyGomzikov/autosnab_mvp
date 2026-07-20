from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.routers import diadoc as diadoc_router


def _client() -> TestClient:
    app = FastAPI()
    app.include_router(diadoc_router.router, prefix="/api/v1")
    return TestClient(app)


def test_diadoc_admin_endpoints_require_configured_key(monkeypatch):
    monkeypatch.setattr(diadoc_router.settings, "diadoc_admin_api_key", "secret-key")
    monkeypatch.setattr(diadoc_router.settings, "bot_api_shared_secret", None)
    client = _client()

    unauthorized = client.get("/api/v1/diadoc/scheduler/status")
    authorized = client.get(
        "/api/v1/diadoc/scheduler/status",
        headers={"X-Diadoc-Api-Key": "secret-key"},
    )

    assert unauthorized.status_code == 401
    assert authorized.status_code == 200


def test_oauth_callback_starts_scheduler_immediately(monkeypatch):
    calls = {"exchange": 0, "scheduler": 0}

    def fake_exchange(code: str, state: str):
        calls["exchange"] += 1
        assert code == "code-1"
        assert state == "state-1"
        return {"access_token": "token"}

    def fake_start() -> bool:
        calls["scheduler"] += 1
        return True

    monkeypatch.setattr(diadoc_router, "exchange_authorization_code", fake_exchange)
    monkeypatch.setattr(diadoc_router, "start_diadoc_scheduler", fake_start)
    client = _client()

    response = client.get(
        "/api/v1/diadoc/oauth/callback",
        params={"code": "code-1", "state": "state-1"},
    )

    assert response.status_code == 200
    assert calls == {"exchange": 1, "scheduler": 1}
    assert "Автоматическая синхронизация запущена" in response.text
