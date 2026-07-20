from __future__ import annotations

from app.config import settings
from app.services import diadoc_sync_service as sync_service


class FakeClient:
    def get_box(self, *, box_id: str):
        return {"BoxIdGuid": box_id, "Title": "Test box"}

    def get_document_types(self, *, box_id: str):
        assert box_id
        return {"DocumentTypes": [{"Name": "UniversalTransferDocument"}]}


def _configure(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "diadoc_integration_enabled", True)
    monkeypatch.setattr(settings, "diadoc_box_id", "11111111-1111-1111-1111-111111111111")
    monkeypatch.setattr(settings, "diadoc_documents_dir", str(tmp_path / "diadoc"))
    monkeypatch.setattr(settings, "google_sheets_enabled", False)
    monkeypatch.setattr(sync_service, "DiadocClient", FakeClient)
    monkeypatch.setattr(
        sync_service,
        "get_diadoc_oauth_status",
        lambda: {"authorized": True},
    )


def test_preflight_is_ready_when_required_diadoc_checks_pass(monkeypatch, tmp_path):
    _configure(monkeypatch, tmp_path)

    result = sync_service.run_diadoc_preflight()

    assert result["ready"] is True
    assert result["box"]["title"] == "Test box"
    assert result["document_types_count"] == 1


def test_preflight_requires_google_oauth_when_sheets_are_enabled(monkeypatch, tmp_path):
    _configure(monkeypatch, tmp_path)
    monkeypatch.setattr(settings, "google_sheets_enabled", True)
    monkeypatch.setattr(settings, "google_target_spreadsheet_id", "sheet-id")
    monkeypatch.setattr(settings, "google_target_sheet_name", "Накладная")
    monkeypatch.setattr(
        sync_service,
        "get_google_oauth_status",
        lambda: {"authorized": False, "error": "expired"},
    )

    result = sync_service.run_diadoc_preflight()

    assert result["ready"] is False
    google_check = next(item for item in result["checks"] if item["name"] == "google_oauth")
    assert google_check["required"] is True
    assert google_check["ready"] is False
