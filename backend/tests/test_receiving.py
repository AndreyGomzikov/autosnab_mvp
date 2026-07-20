import json
import os
import sys
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ["DATABASE_URL"] = "sqlite:///:memory:"
os.environ["BOT_API_SHARED_SECRET"] = ""

from app.db.session import Base, get_db  # noqa: E402
from app.main import app  # noqa: E402
from app.config import settings  # noqa: E402
settings.bot_api_shared_secret = None
from app.models import *  # noqa: F401,F403,E402

engine = create_engine(
    "sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool
)
TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


@pytest.fixture(autouse=True)
def setup_db():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    yield
    Base.metadata.drop_all(bind=engine)


def override_get_db():
    db = TestingSessionLocal()
    try:
        yield db
    finally:
        db.close()


app.dependency_overrides[get_db] = override_get_db
client = TestClient(app)


def _wait_for_trace(trace_id: str, timeout: float = 2.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        response = client.get(f"/api/v1/invoice-review/upload-trace/{trace_id}")
        assert response.status_code == 200
        data = response.json()
        if data.get("completed"):
            return data
        time.sleep(0.05)
    raise AssertionError(f"trace {trace_id} did not complete within {timeout} seconds")


def _start_receiving() -> int:
    payload = {
        "request_id": "REQ-1",
        "order_number": "01TCPC4P-000001",
        "venue": "Добрая столовая",
        "supplier": "Питер Кельн",
        "delivery_address": "ул. Тверская",
        "chat_id": "123",
        "user_id": "456",
        "order_items": [
            {"name": "Молоко кокосовое Aroy-D 400 мл", "quantity": 5, "unit": "шт", "price": 250},
            {"name": "Уксус винный белый PONTI 1 л", "quantity": 1, "unit": "шт", "price": 491.6},
        ],
    }
    response = client.post("/api/v1/receiving/start", json=payload)
    assert response.status_code == 200
    return response.json()["id"]


def test_start_receiving():
    receiving_id = _start_receiving()
    assert receiving_id == 1


def test_compare_invoice_detects_quantity_and_price_mismatch():
    receiving_id = _start_receiving()
    response = client.post(
        f"/api/v1/receiving/{receiving_id}/compare-invoice",
        json={
            "invoice_number": "1056",
            "invoice_date": "2026-06-12",
            "supplier_legal_name": "ООО Питер Кельн",
            "items": [
                {"name": "Молоко кокосовое Aroy-D 400 мл", "quantity": 4, "unit": "шт", "price": 250},
                {"name": "Уксус винный белый PONTI 1 л", "quantity": 1, "unit": "шт", "price": 515},
                {"name": "Соус BBQ", "quantity": 1, "unit": "шт", "price": 120},
            ],
        },
    )
    assert response.status_code == 200
    data = response.json()
    assert data["quantity_mismatch"] == 1
    assert data["price_mismatch"] == 1
    assert data["extra"] == 1


def test_accounting_payload_after_confirm():
    receiving_id = _start_receiving()
    client.post(
        f"/api/v1/receiving/{receiving_id}/documents",
        json={"file_id": "file_1", "file_type": "photo", "raw_text": "Накладная 1056"},
    )
    client.post(
        f"/api/v1/receiving/{receiving_id}/compare-invoice",
        json={
            "invoice_number": "1056",
            "invoice_date": "2026-06-12",
            "supplier_legal_name": "ООО Питер Кельн",
            "items": [
                {"name": "Молоко кокосовое Aroy-D 400 мл", "quantity": 5, "unit": "шт", "price": 250},
                {"name": "Уксус винный белый PONTI 1 л", "quantity": 1, "unit": "шт", "price": 491.6},
            ],
        },
    )
    confirm = client.post(
        f"/api/v1/receiving/{receiving_id}/confirm",
        json={"confirmed": True, "partial": False, "comment": "Все принято"},
    )
    assert confirm.status_code == 200
    payload = client.get(f"/api/v1/receiving/{receiving_id}/accounting-payload")
    assert payload.status_code == 200
    data = payload.json()
    assert data["requestId"] == "REQ-1"
    assert data["invoice"]["number"] == "1056"
    assert len(data["items"]) == 2


def test_accounting_mapping():
    response = client.post(
        "/api/v1/accounting/mappings",
        json={
            "venue": "Добрая столовая",
            "supplier_product_name": "Молоко кокосовое Aroy-D 400 мл",
            "normalized_product_name": "молоко кокосовое",
            "accounting_product_id": "iiko-123",
            "accounting_product_name": "Молоко кокосовое",
            "unit": "шт",
        },
    )
    assert response.status_code == 200
    assert response.json()["accounting_product_id"] == "iiko-123"


def test_mvp2_text_correction_accepts_partial_quantity():
    receiving_id = _start_receiving()
    client.post(
        f"/api/v1/receiving/{receiving_id}/compare-invoice",
        json={
            "invoice_number": "1056",
            "invoice_date": "2026-06-12",
            "supplier_legal_name": "ООО Питер Кельн",
            "items": [
                {"name": "Молоко кокосовое Aroy-D 400 мл", "quantity": 4, "unit": "шт", "price": 250},
                {"name": "Уксус винный белый PONTI 1 л", "quantity": 1, "unit": "шт", "price": 491.6},
            ],
        },
    )
    response = client.post(
        f"/api/v1/receiving/{receiving_id}/corrections/text",
        json={"text": "Молоко кокосовое пришло 4 штуки, подтвердить фактическое количество"},
    )
    assert response.status_code == 200
    items = response.json()["items"]
    milk = next(item for item in items if "Молоко" in (item["item_name_from_order"] or ""))
    assert milk["status"] == "accepted"
    assert milk["received_quantity"] == 4


def test_mvp2_crossed_out_and_extra_processing():
    receiving_id = _start_receiving()
    response = client.post(
        f"/api/v1/receiving/{receiving_id}/compare-invoice",
        json={
            "items": [
                {"name": "Молоко кокосовое Aroy-D 400 мл", "quantity": 5, "unit": "шт", "price": 250},
                {"name": "Соус BBQ", "quantity": 1, "unit": "шт", "price": 120, "crossed_out": True},
            ],
        },
    )
    assert response.status_code == 200
    data = response.json()
    assert data["manual_review"] >= 1
    assert any(item["status"] == "crossed_out" for item in data["items"])


def _prepare_confirmed_receiving() -> int:
    receiving_id = _start_receiving()
    client.post(
        f"/api/v1/receiving/{receiving_id}/documents",
        json={"file_id": "file_1", "file_type": "photo", "raw_text": "Накладная 1056"},
    )
    client.post(
        f"/api/v1/receiving/{receiving_id}/compare-invoice",
        json={
            "invoice_number": "1056",
            "invoice_date": "2026-06-12",
            "supplier_legal_name": "ООО Питер Кельн",
            "items": [
                {"name": "Молоко кокосовое Aroy-D 400 мл", "quantity": 5, "unit": "шт", "price": 250},
                {"name": "Уксус винный белый PONTI 1 л", "quantity": 1, "unit": "шт", "price": 491.6},
            ],
        },
    )
    confirm = client.post(
        f"/api/v1/receiving/{receiving_id}/confirm",
        json={"confirmed": True, "partial": False, "comment": "Все принято"},
    )
    assert confirm.status_code == 200
    return receiving_id


def test_mvp2_cannot_send_to_accounting_before_confirm():
    receiving_id = _start_receiving()
    send_response = client.post(
        f"/api/v1/accounting/receivings/{receiving_id}/send",
        json={"target_system": "iiko", "dry_run": False},
    )
    assert send_response.status_code == 400
    assert "после подтверждения" in send_response.json()["detail"]


def test_mvp2_dry_run_does_not_mark_sent_to_accounting():
    receiving_id = _prepare_confirmed_receiving()
    send_response = client.post(
        f"/api/v1/accounting/receivings/{receiving_id}/send",
        json={"target_system": "iiko", "dry_run": True, "comment": "Тестовая подготовка"},
    )
    assert send_response.status_code == 200
    assert send_response.json()["status"] == "prepared"
    receiving = client.get(f"/api/v1/receiving/{receiving_id}")
    assert receiving.status_code == 200
    assert receiving.json()["status"] == "confirmed_full"


def test_mvp2_send_to_accounting_and_export_csv():
    receiving_id = _prepare_confirmed_receiving()
    send_response = client.post(
        f"/api/v1/accounting/receivings/{receiving_id}/send",
        json={"target_system": "iiko", "dry_run": False, "comment": "Передано из MVP-2"},
    )
    assert send_response.status_code == 200
    assert send_response.json()["status"] == "sent_mock"
    exports = client.get("/api/v1/accounting/exports")
    assert exports.status_code == 200
    assert len(exports.json()) == 1
    csv_response = client.post("/api/v1/receiving/export/google-sheets-mvp")
    assert csv_response.status_code == 200
    assert csv_response.json()["items_csv"].endswith("priemka_pozicii.csv")


def test_invoice_history_and_html_view():
    receiving_id = _prepare_confirmed_receiving()
    docs = client.get(f"/api/v1/receiving/{receiving_id}/documents")
    assert docs.status_code == 200
    assert len(docs.json()["documents"]) == 1
    document_id = docs.json()["documents"][0]["id"]

    history = client.get("/api/v1/documents/history")
    assert history.status_code == 200
    assert len(history.json()["documents"]) == 1

    page = client.get(f"/api/v1/documents/{document_id}/view")
    assert page.status_code == 200
    assert "Накладная" in page.text
    assert "Добрая столовая" in page.text


def test_iiko_payload_and_send():
    receiving_id = _prepare_confirmed_receiving()
    payload = client.get(f"/api/v1/iiko/receivings/{receiving_id}/payload")
    assert payload.status_code == 200
    assert payload.json()["externalNumber"] == "01TCPC4P-000001"
    assert payload.json()["source"] == "autosnab_iiko_adapter"

    send = client.post(
        f"/api/v1/iiko/receivings/{receiving_id}/send",
        json={"target_system": "iiko", "dry_run": True},
    )
    assert send.status_code == 200
    assert send.json()["status"] == "iiko_prepared"

    exports = client.get("/api/v1/iiko/exports")
    assert exports.status_code == 200
    assert len(exports.json()["exports"]) == 1


def test_discrepancy_analytics_and_supplier_control():
    receiving_id = _start_receiving()
    response = client.post(
        f"/api/v1/receiving/{receiving_id}/compare-invoice",
        json={
            "invoice_number": "1056",
            "invoice_date": "2026-06-12",
            "supplier_legal_name": "ООО Другой поставщик",
            "items": [
                {"name": "Молоко кокосовое Aroy-D 400 мл", "quantity": 4, "unit": "шт", "price": 250},
                {"name": "Соус BBQ", "quantity": 1, "unit": "шт", "price": 120},
            ],
        },
    )
    assert response.status_code == 200

    analytics = client.get("/api/v1/analytics/discrepancies")
    assert analytics.status_code == 200
    assert analytics.json()["totals"]["problem_items"] >= 1

    control = client.get("/api/v1/suppliers/control")
    assert control.status_code == 200
    suppliers = control.json()["suppliers"]
    assert suppliers[0]["supplier"] == "Питер Кельн"
    assert suppliers[0]["control_status"] in {"watch", "control_required"}


def test_mvp4_invoice_review_sheet_preview_and_send():
    response = client.post(
        "/api/v1/invoice-review/upload",
        json={
            "file_id": "photo_123",
            "file_type": "photo",
            "file_url": "https://example.test/invoice.jpg",
            "raw_text": "Накладная 777 от 2026-06-19 ООО Питер Кельн",
            "supplier": "Питер Кельн",
            "supplier_legal_name": "ООО Питер Кельн",
            "invoice_date": "2026-06-19",
            "invoice_number": "777",
            "venue": "Добрая столовая",
            "delivery_address": "ул. Тверская",
            "iiko_default_store_id": "STORE-001",
            "iiko_supplier_id": "SUP-001",
            "document_number": "777",
            "incoming_date": "2026-06-19",
            "items": [
                {"name": "Молоко кокосовое Aroy-D 400 мл", "quantity": 5, "unit": "шт", "price": 250, "sum": 1250, "vat": "20%", "product_article": "MILK-001", "amount_unit": "шт", "line_number": 1},
                {"name": "Уксус винный белый PONTI 1 л", "quantity": 1, "unit": "шт", "price": 491.6, "sum": 491.6, "product_article": "VINEGAR-001", "amount_unit": "шт", "line_number": 2},
            ],
        },
    )
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "ready"
    review_id = data["review_id"]

    sheet = client.get(f"/api/v1/invoice-review/{review_id}/sheet")
    assert sheet.status_code == 200
    assert sheet.json()["action"]["button_label"] == "Подтвердить и отправить в iiko"
    assert "Накладные" in sheet.json()["sheets"]

    csv_response = client.get(f"/api/v1/invoice-review/{review_id}/sheet.csv")
    assert csv_response.status_code == 200
    assert "Накладная" in csv_response.text

    preview = client.get(f"/api/v1/invoice-review/{review_id}/preview?target_organization=Добрая%20столовая&target_warehouse=Основной%20склад")
    assert preview.status_code == 200
    assert preview.json()["target_system"] == "iiko"
    assert preview.json()["invoice"]["totalSum"] == 1741.6

    send = client.post(
        f"/api/v1/invoice-review/{review_id}/confirm-send",
        json={
            "approved": True,
            "dry_run": False,
            "target_organization": "Добрая столовая",
            "target_warehouse": "Основной склад",
            "target_warehouse_id": "STORE-001",
            "approved_by": "tester@example.test",
            "comment": "Проверено вручную",
        },
    )
    assert send.status_code == 200
    assert send.json()["status"] == "iiko_sent_mock"
    assert send.json()["payload"]["preview"]["target"]["organization"] == "Добрая столовая"
    assert "<document>" in send.json()["payload"]["iikoXml"]


def test_upload_page_shows_extraction_method_selector():
    response = client.get("/api/v1/invoice-review/upload-page")

    assert response.status_code == 200
    assert 'name="extraction_method"' in response.text
    assert 'value="google_ocr"' in response.text
    assert 'value="mineru"' in response.text
    assert 'value="hybrid"' in response.text


def test_upload_photo_passes_selected_extraction_method(monkeypatch, tmp_path):
    from app.routers import invoice_review as invoice_review_router

    captured = {}
    monkeypatch.setattr(invoice_review_router.settings, "uploaded_invoices_dir", str(tmp_path))

    def fake_extract(file_path, fallback_filename=None, extraction_method=None, on_log=None):
        captured["file_path"] = file_path
        captured["fallback_filename"] = fallback_filename
        captured["extraction_method"] = extraction_method
        return {
            "provider": "mineru",
            "selected_method": "hybrid",
            "raw_text": "ООО Питер Кельн",
            "pages": 1,
            "pipeline_logs": [
                {
                    "stage": "mineru_complete",
                    "status": "ok",
                    "message": "MinerU вернул полезные данные.",
                    "details": {"items_count": 0},
                }
            ],
            "payload": {
                "supplier": "ООО Питер Кельн",
                "supplier_legal_name": "ООО Питер Кельн",
                "invoice_number": "123",
                "invoice_date": "2026-07-04",
                "venue": "Добрая столовая",
                "items": [],
                "parser_provider": "mineru",
                "parser_notes": [],
            },
        }

    monkeypatch.setattr(invoice_review_router, "extract_invoice_document", fake_extract)

    response = client.post(
        "/api/v1/invoice-review/upload-photo",
        files={"file": ("invoice.jpg", b"fake-image", "image/jpeg")},
        data={
            "create_google_sheet": "false",
            "extraction_method": "hybrid",
        },
    )

    assert response.status_code == 200
    assert captured["fallback_filename"] == "invoice.jpg"
    assert captured["extraction_method"] == "hybrid"
    assert response.json()["ocr"]["selected_method"] == "hybrid"
    assert response.json()["pipeline_logs"]


def test_upload_photo_stops_and_returns_pipeline_logs_for_empty_result(monkeypatch, tmp_path):
    from app.routers import invoice_review as invoice_review_router

    monkeypatch.setattr(invoice_review_router.settings, "uploaded_invoices_dir", str(tmp_path))

    def fake_extract(file_path, fallback_filename=None, extraction_method=None, on_log=None):
        return {
            "provider": "openai_empty_evidence",
            "selected_method": "openai",
            "raw_text": "",
            "pages": 0,
            "error": "Перед OpenAI parser не удалось получить текст или structured evidence.",
            "stop_recommended": True,
            "retry_recommended_method": "openai",
            "retry_recommended_label": "OpenAI structured parser",
            "pipeline_logs": [
                {
                    "stage": "collect_evidence_complete",
                    "status": "warning",
                    "message": "Evidence для OpenAI parser пустой.",
                    "details": {"raw_text_length": 0},
                }
            ],
            "payload": {
                "supplier": None,
                "items": [],
                "parser_provider": "manual_review_empty_sheet",
                "parser_notes": [],
            },
        }

    monkeypatch.setattr(invoice_review_router, "extract_invoice_document", fake_extract)

    response = client.post(
        "/api/v1/invoice-review/upload-photo",
        files={"file": ("invoice.jpg", b"fake-image", "image/jpeg")},
        data={
            "create_google_sheet": "false",
            "extraction_method": "openai",
        },
    )

    assert response.status_code == 422
    assert response.json()["detail"]["pipeline_logs"][0]["stage"] == "collect_evidence_complete"
    assert response.json()["detail"]["retry_recommended_method"] == "openai"


def test_upload_trace_endpoint_returns_live_trace(monkeypatch, tmp_path):
    from app.routers import invoice_review as invoice_review_router

    monkeypatch.setattr(invoice_review_router.settings, "uploaded_invoices_dir", str(tmp_path))

    def fake_extract(file_path, fallback_filename=None, extraction_method=None, on_log=None):
        if on_log:
            on_log(
                {
                    "stage": "openai_request_start",
                    "status": "running",
                    "message": "Отправляю evidence в OpenAI parser.",
                    "details": {"raw_text_length": 123},
                }
            )
        return {
            "provider": "openai",
            "selected_method": "openai",
            "raw_text": "evidence",
            "pages": 1,
            "evidence": {
                "logical_document_id": "document-1",
                "evidence_version": "1.0",
                "pages": 1,
            },
            "pipeline_logs": [
                {
                    "stage": "openai_request_start",
                    "status": "running",
                    "message": "Отправляю evidence в OpenAI parser.",
                    "details": {"raw_text_length": 123},
                }
            ],
            "payload": {
                "supplier": "ООО Питер Кельн",
                "supplier_legal_name": "ООО Питер Кельн",
                "invoice_number": "123",
                "invoice_date": "2026-07-04",
                "venue": "Добрая столовая",
                "items": [],
                "parser_provider": "openai",
                "parser_notes": [],
            },
        }

    monkeypatch.setattr(invoice_review_router, "extract_invoice_document", fake_extract)

    trace_id = "test-trace-1"
    response = client.post(
        "/api/v1/invoice-review/upload-photo",
        files={"file": ("invoice.jpg", b"fake-image", "image/jpeg")},
        data={
            "create_google_sheet": "false",
            "extraction_method": "openai",
            "upload_trace_id": trace_id,
        },
    )

    assert response.status_code == 200
    trace_data = _wait_for_trace(trace_id)
    assert trace_data["completed"] is True
    assert trace_data["trace_version"] == "1.0"
    assert trace_data["metadata"]["logical_document_id"] == "document-1"
    assert trace_data["logs"][0]["stage"] == "openai_request_start"


def test_upload_photo_live_returns_trace_id_and_background_result(monkeypatch, tmp_path):
    from app.routers import invoice_review as invoice_review_router

    monkeypatch.setattr(invoice_review_router.settings, "uploaded_invoices_dir", str(tmp_path))

    def fake_extract(file_path, fallback_filename=None, extraction_method=None, on_log=None):
        if on_log:
            on_log(
                {
                    "stage": "document_received",
                    "status": "ok",
                    "message": "Документ получен backend-сервисом.",
                    "details": {"selected_method": extraction_method or "openai"},
                }
            )
        return {
            "provider": "openai",
            "selected_method": "openai",
            "raw_text": "evidence",
            "pages": 1,
            "evidence": {
                "logical_document_id": "document-1",
                "evidence_version": "1.0",
                "pages": 1,
            },
            "pipeline_logs": [
                {
                    "stage": "document_received",
                    "status": "ok",
                    "message": "Документ получен backend-сервисом.",
                    "details": {"selected_method": extraction_method or "openai"},
                }
            ],
            "payload": {
                "supplier": "ООО Питер Кельн",
                "supplier_legal_name": "ООО Питер Кельн",
                "invoice_number": "123",
                "invoice_date": "2026-07-04",
                "venue": "Добрая столовая",
                "items": [],
                "parser_provider": "openai",
                "parser_notes": [],
            },
        }

    monkeypatch.setattr(invoice_review_router, "extract_invoice_document", fake_extract)
    monkeypatch.setattr(invoice_review_router, "create_real_google_sheet_for_review", lambda *args, **kwargs: {
        "spreadsheet_id": "sheet-1",
        "spreadsheet_url": "https://example.test/sheet-1",
    })

    response = client.post(
        "/api/v1/invoice-review/upload-photo-live",
        files={"file": ("invoice.jpg", b"fake-image", "image/jpeg")},
        data={
            "create_google_sheet": "true",
            "extraction_method": "openai",
            "upload_trace_id": "live-trace-1",
        },
    )

    assert response.status_code == 200
    assert response.json()["trace_id"] == "live-trace-1"

    trace_data = _wait_for_trace("live-trace-1")
    assert trace_data["completed"] is True
    assert trace_data["metadata"]["evidence_version"] == "1.0"
    assert trace_data["result"]["google_spreadsheet_url"] == "https://example.test/sheet-1"
    assert trace_data["result"]["trace_metadata"]["logical_document_id"] == "document-1"


def test_upload_photo_live_surfaces_readonly_database_hint(monkeypatch, tmp_path):
    from app.routers import invoice_review as invoice_review_router

    monkeypatch.setattr(invoice_review_router.settings, "uploaded_invoices_dir", str(tmp_path))

    def fake_process(**kwargs):
        raise OperationalError(
            "INSERT INTO receivings DEFAULT VALUES",
            {},
            Exception("attempt to write a readonly database"),
        )

    monkeypatch.setattr(invoice_review_router, "_process_invoice_upload", fake_process)

    response = client.post(
        "/api/v1/invoice-review/upload-photo-live",
        files={"file": ("invoice.jpg", b"fake-image", "image/jpeg")},
        data={
            "create_google_sheet": "false",
            "extraction_method": "openai",
            "upload_trace_id": "readonly-db-trace-1",
        },
    )

    assert response.status_code == 200

    trace_data = _wait_for_trace("readonly-db-trace-1")
    assert trace_data["completed"] is True
    assert "SQLite database is read-only" in trace_data["error_message"]
    assert trace_data["logs"][-1]["stage"] == "job_failed"
    assert "SQLite database is read-only" in trace_data["logs"][-1]["details"]["error"]


def test_upload_document_live_preserves_page_order(monkeypatch, tmp_path):
    from app.routers import invoice_review as invoice_review_router

    captured = {}
    monkeypatch.setattr(invoice_review_router.settings, "uploaded_invoices_dir", str(tmp_path))

    def fake_background(**kwargs):
        captured.update(kwargs)
        invoice_review_router.finalize_trace(kwargs["trace_id"])

    monkeypatch.setattr(
        invoice_review_router,
        "_process_invoice_upload_background",
        fake_background,
    )

    response = client.post(
        "/api/v1/invoice-review/upload-document-live",
        files=[
            ("files", ("page-1.jpg", b"first", "image/jpeg")),
            ("files", ("page-2.jpg", b"second", "image/jpeg")),
        ],
        data={
            "create_google_sheet": "false",
            "extraction_method": "openai",
            "upload_trace_id": "multipage-trace-1",
        },
    )

    assert response.status_code == 200
    assert response.json()["pages"] == 2
    trace = _wait_for_trace("multipage-trace-1")
    assert trace["completed"] is True
    assert captured["file_names"] == ["page-1.jpg", "page-2.jpg"]
    assert [Path(path).read_bytes() for path in captured["file_paths"]] == [b"first", b"second"]
    assert captured["file_type"] == "multipage"


def test_bot_upload_returns_unsupported_format_without_crash(monkeypatch, tmp_path):
    monkeypatch.setattr("app.routers.invoice_review.settings.uploaded_invoices_dir", str(tmp_path))

    response = client.post(
        "/api/v1/invoice-review/bot/upload-document-live",
        files=[("files", ("invoice.xml", b"<xml/>", "application/xml"))],
        data={
            "source_user_id": "tg-user-1",
            "source_username": "operator_1",
            "source_channel": "telegram_bot",
            "document_kind": "primary_document",
        },
    )

    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "unsupported_format"
    assert data["trace_id"] is None
    assert data["unsupported_reason"]

    status_response = client.get(f"/api/v1/invoice-review/bot/uploads/{data['upload_id']}")
    assert status_response.status_code == 200
    status_data = status_response.json()
    assert status_data["completed"] is True
    assert status_data["status"] == "unsupported_format"
    assert "backend" in status_data["error_text"]


def test_bot_upload_creates_trace_and_updates_journal(monkeypatch, tmp_path):
    from app.routers import invoice_review as invoice_review_router

    monkeypatch.setattr(invoice_review_router.settings, "uploaded_invoices_dir", str(tmp_path))

    def fake_process(**kwargs):
        response = {
            "review_id": 55,
            "status": "ready",
            "issues": [],
            "next_actions": {"open_sheet": "/api/v1/invoice-review/55/sheet"},
            "google_spreadsheet_error": None,
        }
        invoice_review_router.set_trace_result(kwargs["upload_trace_id"], response)
        invoice_review_router.finalize_trace(kwargs["upload_trace_id"])
        return response

    monkeypatch.setattr(invoice_review_router, "_process_invoice_upload", fake_process)

    response = client.post(
        "/api/v1/invoice-review/bot/upload-document-live",
        files=[("files", ("invoice.jpg", b"fake-image", "image/jpeg"))],
        data={
            "source_user_id": "tg-user-2",
            "source_username": "operator_2",
            "source_channel": "telegram_bot",
            "document_kind": "primary_document",
            "create_google_sheet": "false",
        },
    )

    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "accepted_for_processing"
    assert data["trace_id"]

    trace = _wait_for_trace(data["trace_id"])
    assert trace["completed"] is True
    assert trace["result"]["review_id"] == 55

    status_response = client.get(f"/api/v1/invoice-review/bot/uploads/{data['upload_id']}")
    assert status_response.status_code == 200
    status_data = status_response.json()
    assert status_data["status"] == "transferred_to_review"
    assert status_data["completed"] is True
    assert status_data["review_id"] == 55
    assert status_data["result_code"] == "transferred_to_review"
    assert status_data["next_actions"]["review_id"] == 55


def test_bot_draft_pages_accumulate_across_requests(monkeypatch, tmp_path):
    monkeypatch.setattr("app.routers.invoice_review.settings.uploaded_invoices_dir", str(tmp_path))

    first = client.post(
        "/api/v1/invoice-review/bot/drafts/pages",
        files={"file": ("page-1.jpg", b"first", "image/jpeg")},
        data={"chat_id": "chat-draft-1", "source_user_id": "tg-user-9"},
    )
    assert first.status_code == 200
    first_data = first.json()
    assert first_data["status"] == "collecting"
    assert first_data["pages_count"] == 1
    upload_id = first_data["upload_id"]

    second = client.post(
        "/api/v1/invoice-review/bot/drafts/pages",
        files={"file": ("page-2.jpg", b"second", "image/jpeg")},
        data={"chat_id": "chat-draft-1", "source_user_id": "tg-user-9"},
    )
    assert second.status_code == 200
    second_data = second.json()
    assert second_data["upload_id"] == upload_id
    assert second_data["pages_count"] == 2
    assert second_data["filenames"] == ["page-1.jpg", "page-2.jpg"]

    status_response = client.get(
        "/api/v1/invoice-review/bot/drafts/status",
        params={"chat_id": "chat-draft-1"},
    )
    assert status_response.status_code == 200
    draft = status_response.json()["draft"]
    assert draft["upload_id"] == upload_id
    assert draft["pages_count"] == 2
    assert draft["filenames"] == ["page-1.jpg", "page-2.jpg"]


def test_bot_draft_is_isolated_per_chat(monkeypatch, tmp_path):
    monkeypatch.setattr("app.routers.invoice_review.settings.uploaded_invoices_dir", str(tmp_path))

    client.post(
        "/api/v1/invoice-review/bot/drafts/pages",
        files={"file": ("page-1.jpg", b"first", "image/jpeg")},
        data={"chat_id": "chat-a", "source_user_id": "tg-user-a"},
    )

    other_status = client.get(
        "/api/v1/invoice-review/bot/drafts/status",
        params={"chat_id": "chat-b"},
    )
    assert other_status.status_code == 200
    assert other_status.json()["draft"] is None


def test_bot_draft_reset_clears_pages_before_next_upload(monkeypatch, tmp_path):
    monkeypatch.setattr("app.routers.invoice_review.settings.uploaded_invoices_dir", str(tmp_path))

    client.post(
        "/api/v1/invoice-review/bot/drafts/pages",
        files={"file": ("page-1.jpg", b"first", "image/jpeg")},
        data={"chat_id": "chat-reset-1", "source_user_id": "tg-user-9"},
    )
    reset_response = client.post(
        "/api/v1/invoice-review/bot/drafts/reset",
        data={"chat_id": "chat-reset-1"},
    )
    assert reset_response.status_code == 200
    assert reset_response.json()["status"] == "reset"

    status_response = client.get(
        "/api/v1/invoice-review/bot/drafts/status",
        params={"chat_id": "chat-reset-1"},
    )
    assert status_response.json()["draft"] is None

    fresh = client.post(
        "/api/v1/invoice-review/bot/drafts/pages",
        files={"file": ("page-new.jpg", b"third", "image/jpeg")},
        data={"chat_id": "chat-reset-1", "source_user_id": "tg-user-9"},
    )
    assert fresh.json()["pages_count"] == 1
    assert fresh.json()["filenames"] == ["page-new.jpg"]


def test_bot_draft_reset_without_active_draft_is_a_safe_no_op():
    response = client.post(
        "/api/v1/invoice-review/bot/drafts/reset",
        data={"chat_id": "chat-never-opened"},
    )
    assert response.status_code == 200
    assert response.json()["status"] == "no_active_draft"


def test_bot_draft_finalize_without_pages_returns_422():
    response = client.post(
        "/api/v1/invoice-review/bot/drafts/finalize",
        data={"chat_id": "chat-empty-draft"},
    )
    assert response.status_code == 422


def test_bot_draft_finalize_starts_processing_and_is_visible_via_latest(monkeypatch, tmp_path):
    from app.routers import invoice_review as invoice_review_router

    monkeypatch.setattr(invoice_review_router.settings, "uploaded_invoices_dir", str(tmp_path))

    captured = {}

    def fake_background(**kwargs):
        captured.update(kwargs)
        invoice_review_router.finalize_trace(kwargs["trace_id"])

    monkeypatch.setattr(invoice_review_router, "_process_bot_upload_background", fake_background)

    client.post(
        "/api/v1/invoice-review/bot/drafts/pages",
        files={"file": ("page-1.jpg", b"first", "image/jpeg")},
        data={"chat_id": "chat-finalize-1", "source_user_id": "tg-user-9"},
    )
    client.post(
        "/api/v1/invoice-review/bot/drafts/pages",
        files={"file": ("page-2.jpg", b"second", "image/jpeg")},
        data={"chat_id": "chat-finalize-1", "source_user_id": "tg-user-9"},
    )

    finalize_response = client.post(
        "/api/v1/invoice-review/bot/drafts/finalize",
        data={"chat_id": "chat-finalize-1", "create_google_sheet": "false"},
    )
    assert finalize_response.status_code == 200
    finalize_data = finalize_response.json()
    assert finalize_data["status"] == "accepted_for_processing"
    assert finalize_data["files_count"] == 2
    assert finalize_data["trace_id"]

    assert captured["file_names"] == ["page-1.jpg", "page-2.jpg"]

    _wait_for_trace(finalize_data["trace_id"])

    no_more_draft = client.get(
        "/api/v1/invoice-review/bot/drafts/status",
        params={"chat_id": "chat-finalize-1"},
    )
    assert no_more_draft.json()["draft"] is None

    latest_response = client.get(
        "/api/v1/invoice-review/bot/uploads/latest",
        params={"chat_id": "chat-finalize-1"},
    )
    assert latest_response.status_code == 200
    latest_data = latest_response.json()
    assert latest_data["upload_id"] == finalize_data["upload_id"]
    assert latest_data["status"] == "accepted_for_processing"


def test_bot_uploads_latest_returns_404_when_chat_has_no_uploads():
    response = client.get(
        "/api/v1/invoice-review/bot/uploads/latest",
        params={"chat_id": "chat-with-no-history"},
    )
    assert response.status_code == 404


def test_bot_drafts_pages_rejects_unsupported_format(tmp_path, monkeypatch):
    monkeypatch.setattr("app.routers.invoice_review.settings.uploaded_invoices_dir", str(tmp_path))

    response = client.post(
        "/api/v1/invoice-review/bot/drafts/pages",
        files={"file": ("invoice.xml", b"<xml/>", "application/xml")},
        data={"chat_id": "chat-unsupported-1", "source_user_id": "tg-user-9"},
    )
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "unsupported_format"
    assert data["pages_count"] == 0

    status_response = client.get(
        "/api/v1/invoice-review/bot/drafts/status",
        params={"chat_id": "chat-unsupported-1"},
    )
    assert status_response.json()["draft"] is None


def test_build_review_sheet_backfills_invoice_reference_mapping_for_old_items(monkeypatch):
    from app.services import invoice_review_service
    from app.schemas.invoice_review import InvoiceReviewCreateRequest, RecognizedInvoiceItem

    payload = InvoiceReviewCreateRequest(
        supplier='ООО "ВИКТОРИЯ БАЛТИЯ"',
        supplier_legal_name='ООО "ВИКТОРИЯ БАЛТИЯ"',
        invoice_date="2026-07-04",
        invoice_number="0245",
        venue="Добрая столовая",
        items=[
            RecognizedInvoiceItem(
                name='3 ТОВАР : ШТ. [N+13968 КЕФИР ФЕРМЕРСКИЙ 800Г',
                quantity=1,
                unit="ШТ",
                price=72.9,
            )
        ],
        parser_metadata={
            "upload_status": "Требует проверки",
            "row_status": "Правка вручную",
            "items": [
                {
                    "line_number": 1,
                    "raw_name": '3 ТОВАР : ШТ. [N+13968 КЕФИР ФЕРМЕРСКИЙ 800Г',
                    "clean_name": "КЕФИР ФЕРМЕРСКИЙ",
                    "normalized_name_candidate": "Кефир Фермерский",
                    "package": {"raw": "800Г"},
                    "document_unit": "ШТ",
                    "quantity_document": 1,
                    "quantity_multiplier": 0.8,
                    "accounting_unit_candidate": "л",
                }
            ],
        },
    )
    db = TestingSessionLocal()
    try:
        receiving = invoice_review_service.create_invoice_review(db, payload)
        monkeypatch.setattr(invoice_review_service.settings, "google_sheets_enabled", True)
        monkeypatch.setattr(invoice_review_service.settings, "google_target_spreadsheet_id", "sheet-id")
        monkeypatch.setattr(
            invoice_review_service,
            "load_invoice_reference_catalogs",
            lambda: {
                "products": [{"Наименование": "Кефир", "Код": "01-00017", "Ед. изм.": "л"}],
                "packages": [],
            },
        )

        sheet = invoice_review_service.build_review_sheet(receiving)
        rows = sheet["sheets"][sheet["primary_sheet_name"]]
    finally:
        db.close()

    assert rows[1][16] == "Кефир"
    assert rows[1][17] == "Да"


def test_build_review_sheet_falls_back_to_normalized_name_when_mapping_fields_are_missing():
    from app.services import invoice_review_service
    from app.schemas.invoice_review import InvoiceReviewCreateRequest, RecognizedInvoiceItem

    payload = InvoiceReviewCreateRequest(
        supplier='ООО "ВИКТОРИЯ БАЛТИЯ"',
        supplier_legal_name='ООО "ВИКТОРИЯ БАЛТИЯ"',
        invoice_date="2026-07-04",
        invoice_number="0245",
        venue="Добрая столовая",
        items=[
            RecognizedInvoiceItem(
                name='3 ТОВАР : ШТ. [N+13968 КЕФИР ФЕРМЕРСКИЙ 800Г',
                quantity=1,
                unit="ШТ",
                price=72.9,
            )
        ],
        parser_metadata={
            "upload_status": "Требует проверки",
            "row_status": "Распознано",
            "items": [
                {
                    "line_number": 1,
                    "raw_name": '3 ТОВАР : ШТ. [N+13968 КЕФИР ФЕРМЕРСКИЙ 800Г',
                    "clean_name": "КЕФИР ФЕРМЕРСКИЙ",
                    "normalized_name_candidate": "Кефир Фермерский",
                    "document_unit": "ШТ",
                    "quantity_document": 1,
                    "accounting_quantity_candidate": 0.8,
                    "accounting_unit_candidate": "кг",
                }
            ],
        },
    )
    db = TestingSessionLocal()
    try:
        receiving = invoice_review_service.create_invoice_review(db, payload)
        sheet = invoice_review_service.build_review_sheet(receiving)
        rows = sheet["sheets"][sheet["primary_sheet_name"]]
    finally:
        db.close()

    assert rows[1][16] == "Кефир Фермерский"
    assert rows[1][17] == ""


def test_invoice_register_header_uses_receiving_id_for_document_id():
    from app.services import invoice_review_service
    from app.schemas.invoice_review import InvoiceReviewCreateRequest, RecognizedInvoiceItem

    payload = InvoiceReviewCreateRequest(
        supplier="ООО Поставщик",
        supplier_legal_name="ООО Поставщик",
        invoice_date="2026-07-04",
        invoice_number="A-42",
        venue="Добрая столовая",
        items=[
            RecognizedInvoiceItem(
                name="Молоко",
                quantity=1,
                unit="шт",
                price=100,
            )
        ],
        parser_metadata={},
    )
    db = TestingSessionLocal()
    try:
        receiving = invoice_review_service.create_invoice_review(db, payload)
        document = receiving.documents[-1] if receiving.documents else None
        header_meta = {"document_id": 1}

        header_values = invoice_review_service._invoice_register_header_values(
            receiving,
            document,
            header_meta,
            total_sum=100,
        )
    finally:
        db.close()

    assert header_values["document_id"] == receiving.id


def test_mvp4_requires_manual_approval_before_send():
    response = client.post(
        "/api/v1/invoice-review/upload",
        json={
            "supplier": "Питер Кельн",
            "invoice_date": "2026-06-19",
            "invoice_number": "778",
            "venue": "Добрая столовая",
            "items": [{"name": "Сахар", "quantity": 1, "unit": "шт", "price": 100}],
        },
    )
    assert response.status_code == 200
    review_id = response.json()["review_id"]
    send = client.post(f"/api/v1/invoice-review/{review_id}/confirm-send", json={"approved": False})
    assert send.status_code == 400
    assert "подтвердить" in send.json()["detail"]


def test_mvp4_real_ocr_endpoint_falls_back_to_manual_review_without_ocr_credentials(monkeypatch):
    from app.services import document_extraction_service

    def fake_ocr(_file_path):
        raise document_extraction_service.OcrConfigurationError("OCR disabled in test")

    old_backend = document_extraction_service.settings.document_extraction_backend
    old_fallback = document_extraction_service.settings.document_extraction_fallback_to_ocr
    document_extraction_service.settings.document_extraction_backend = "ocr"
    document_extraction_service.settings.document_extraction_fallback_to_ocr = True
    monkeypatch.setattr(document_extraction_service, "recognize_invoice_image", fake_ocr)
    try:
        result = document_extraction_service.extract_invoice_document("invoice.jpg", "invoice.jpg")
    finally:
        document_extraction_service.settings.document_extraction_backend = old_backend
        document_extraction_service.settings.document_extraction_fallback_to_ocr = old_fallback

    assert result["provider"] == "manual_review_fallback"
    assert result["error"]
    assert result["payload"]["parser_provider"] == "manual_review_empty_sheet"


def test_mvp4_sync_sheet_corrections_before_iiko_send():
    response = client.post(
        "/api/v1/invoice-review/upload",
        json={
            "supplier": "Питер Кельн",
            "invoice_date": "2026-06-19",
            "invoice_number": "779",
            "venue": "Добрая столовая",
            "items": [{"name": "Сахар", "quantity": 1, "unit": "шт", "price": 100}],
        },
    )
    assert response.status_code == 200
    review_id = response.json()["review_id"]
    send = client.post(
        f"/api/v1/invoice-review/{review_id}/sync-sheet-and-confirm-send",
        json={
            "approved": True,
            "dry_run": False,
            "target_organization": "Добрая столовая",
            "target_warehouse": "Основной склад",
            "target_warehouse_id": "STORE-001",
            "approved_by": "sheet-user@example.test",
            "supplier": "ООО Питер Кельн",
            "supplier_legal_name": "ООО Питер Кельн",
            "invoice_date": "2026-06-19",
            "invoice_number": "779",
            "venue": "Добрая столовая",
            "iiko_default_store_id": "STORE-001",
            "iiko_supplier_id": "SUP-001",
            "items": [
                {"name": "Сахар ванильный", "quantity": 2, "unit": "шт", "price": 110, "sum": 220, "vat": "20%", "product_article": "SUGAR-001", "amount_unit": "шт", "line_number": 1}
            ],
        },
    )
    assert send.status_code == 200
    data = send.json()
    assert data["status"] == "iiko_sent_mock"
    assert data["payload"]["preview"]["items"][0]["name"] == "Сахар ванильный"
    assert data["payload"]["preview"]["items"][0]["quantity"] == 2


def test_mvp4_deterministic_parser_parses_ocr_text():
    from app.services.invoice_parser_service import extract_invoice_payload_with_fallback

    raw_text = """
    ООО Питер Кельн
    Накладная №12345 от 19.06.2026
    Молоко кокосовое Aroy-D 400 мл 5 шт 250 1250
    """
    payload = extract_invoice_payload_with_fallback(raw_text, "invoice.jpg")
    assert payload["parser_provider"] == "deterministic_parser"
    assert payload["supplier"] == "ООО Питер Кельн"
    assert payload["invoice_number"] == "12345"
    assert payload["invoice_date"] == "2026-06-19"
    assert payload["items"][0]["name"] == "Молоко кокосовое Aroy-D 400 мл"


def test_mvp4_upload_photo_response_can_include_parser_metadata(monkeypatch):
    from app.services import document_extraction_service

    old_backend = document_extraction_service.settings.document_extraction_backend
    document_extraction_service.settings.document_extraction_backend = "mineru"
    monkeypatch.setattr(
        document_extraction_service,
        "_run_mineru_command",
        lambda _file_path: {
            "raw_text": "ООО Питер Кельн\nНакладная №555 от 19.06.2026\nСахар 2 шт 100 200",
            "pages": 1,
            "confidence": 0.98,
            "header": {
                "supplier": "ООО Питер Кельн",
                "invoice_number": "555",
                "invoice_date": "2026-06-19",
                "venue": "Добрая столовая",
            },
            "items": [
                {"name": "Сахар", "quantity": 2, "unit": "шт", "price": 100, "sum": 200, "confidence": 0.91}
            ],
        },
    )
    try:
        result = document_extraction_service.extract_invoice_document("invoice.jpg", "invoice.jpg")
    finally:
        document_extraction_service.settings.document_extraction_backend = old_backend

    assert result["provider"] == "mineru"
    assert result["payload"]["parser_provider"] == "mineru"
    assert result["payload"]["supplier"] == "ООО Питер Кельн"
    assert result["payload"]["items"][0]["name"] == "Сахар"


def test_mineru_document_extraction_response_is_wired_through_upload(monkeypatch):
    from app.services import document_extraction_service

    old_backend = document_extraction_service.settings.document_extraction_backend
    document_extraction_service.settings.document_extraction_backend = "mineru"
    monkeypatch.setattr(
        document_extraction_service,
        "_run_mineru_command",
        lambda _file_path: {
            "raw_text": "ООО Питер Кельн\nНакладная №555 от 19.06.2026\nСахар 2 шт 100 200",
            "pages": 2,
            "confidence": 0.97,
            "header": {
                "supplier": "ООО Питер Кельн",
                "invoice_number": "555",
                "invoice_date": "2026-06-19",
                "venue": "Добрая столовая",
                "delivery_address": "ул. Тверская",
            },
            "items": [
                {"name": "Сахар", "quantity": 2, "unit": "шт", "price": 100, "sum": 200, "confidence": 0.91}
            ],
        },
    )
    try:
        result = document_extraction_service.extract_invoice_document("invoice.jpg", "invoice.jpg")
    finally:
        document_extraction_service.settings.document_extraction_backend = old_backend

    assert result["provider"] == "mineru"
    assert result["pages"] == 2
    assert result["payload"]["parser_provider"] == "mineru"
    assert result["payload"]["parser_notes"]



def test_invoice_review_sheet_does_not_guess_supplier_inn_from_raw_text():
    response = client.post(
        "/api/v1/invoice-review/upload",
        json={
            "raw_text": """
            Страница 2
            Товарная накладная имеет приложение на
            Всего отпущено на сумму
            ООО "ЛИР", ИНН 3906400288
            """,
            "document_form": "ТОРГ-12",
            "total_sum": 16351.45,
            "items": [
                {
                    "name": "Окорок \"По-тамбовски\" к/в в/у",
                    "quantity": 4.058,
                    "unit": "кг",
                    "price": 702.0,
                    "sum": 2848.72,
                    "vat": "7%",
                    "vat_percent": 7.0,
                    "vat_sum": 199.41,
                }
            ],
        },
    )

    assert response.status_code == 200
    review_id = response.json()["review_id"]
    sheet = client.get(f"/api/v1/invoice-review/{review_id}/sheet")

    assert sheet.status_code == 200
    rows = sheet.json()["sheets"]["Накладные"]
    values = dict(zip(rows[0], rows[1], strict=True))
    assert values["Дата документа"] == ""
    assert values["№ Документа"] == ""
    assert values["Поставщик"] == ""
    assert values["Наименование товара из документа"] == "Окорок \"По-тамбовски\" к/в в/у"
    assert values["Ед.изм."] == "кг"
    assert values["Ставка НДС %"] == "7%"
    assert values["Сумма накладной"] == 16351.45

def test_mvp4_auto_fills_iiko_fields_from_references(monkeypatch):
    from app.services import iiko_reference_mapping_service

    def fake_context(force_refresh=False):
        return {
            "status": "ready",
            "context": {
                "suppliers": [{"id": "SUP-001", "name": "ООО Питер Кельн", "code": "PK"}],
                "stores": [{"id": "STORE-001", "name": "Добрая столовая"}],
                "products": [{"id": "PROD-001", "name": "Сахар ванильный", "num": "SUGAR-001", "taxCategory": "TAX-20"}],
                "units": [{"id": "шт", "name": "шт", "code": "шт"}],
                "taxes": [{"id": "TAX-20", "name": "НДС 20%", "vatPercent": 20}],
            },
        }

    monkeypatch.setattr(iiko_reference_mapping_service, "get_iiko_reference_context", fake_context)

    response = client.post(
        "/api/v1/invoice-review/upload",
        json={
            "supplier": "Питер Кельн",
            "invoice_date": "2026-06-19",
            "invoice_number": "780",
            "document_form": "ТОРГ-12",
            "venue": "Добрая столовая",
            "items": [
                {"name": "Сахар ванильный", "quantity": 2, "unit": "шт", "price": 110},
                {"name": "Молоко", "quantity": 1, "unit": "л", "price": 90},
            ],
        },
    )
    assert response.status_code == 200
    review_id = response.json()["review_id"]
    sheet = client.get(f"/api/v1/invoice-review/{review_id}/sheet")
    assert sheet.status_code == 200
    rows = sheet.json()["sheets"]["Накладные"]
    assert "Служебные поля iiko" not in sheet.json()["sheets"]
    assert rows[0][0] == "Статус загрузки"
    assert "Время загрузки документа" in rows[0]
    assert "ID документа" in rows[0]
    assert "Индикатор дубля документа" in rows[0]
    assert "Поставщик" in rows[0]
    assert "Наименование товара из документа" in rows[0]
    assert "Госсистемы" in rows[0]
    assert "ЕГАИС" not in rows[0]
    assert "Меркурий" not in rows[0]
    assert "Честный знак" not in rows[0]
    assert "Кол-во из документа" in rows[0]
    assert "Дата приема" in rows[0]
    assert "Статус строки" in rows[0]
    first = dict(zip(rows[0], rows[1], strict=True))
    second = dict(zip(rows[0], rows[2], strict=True))
    assert first["Время загрузки документа"] != ""
    assert first["ID документа"] == review_id
    assert first["Форма документа"] == "ТОРГ-12"
    assert first["№ Документа"] == "780"
    assert first["Дата документа"] == "2026-06-19"
    assert first["Поставщик"] == "Питер Кельн"
    assert first["Торговая точка"] == "Добрая столовая"
    for field in (
        "Статус загрузки",
        "Время загрузки документа",
        "ID документа",
        "Индикатор дубля документа",
        "Форма документа",
        "Дата документа",
        "№ Документа",
        "Поставщик",
        "ИНН Поставщика",
        "Грузополучатель",
        "Получатель",
        "Торговая точка",
        "Склад",
        "Основание",
        "Сумма накладной",
    ):
        assert second[field] == ""
    assert first["Общая стоимость"] != ""
    assert second["Общая стоимость"] != ""
    assert "Статус проверки" not in rows[0]
    assert "Что исправить" not in rows[0]
    assert first["Наименование товара из документа"] == "Сахар ванильный"
    assert first["Госсистемы"] == ""
    assert first["Наименование товара в УС"] == "Сахар ванильный"
    assert first["Ед.изм. в УС"] == "шт"
    assert first["Кол-во в УС"] == 2
    assert first["Цена в УС"] == 110
    assert first["Последняя цена"] == ""
    assert first["Отклонение от цены прайса"] == ""
    assert first["Загрузить в УС"] == ""
    assert first["Статус строки"] in {"Распознано", "Правка вручную", ""}
    assert first["Причина ручной корректировки"] != "41"

    preview = client.get(f"/api/v1/invoice-review/{review_id}/preview")
    assert preview.status_code == 200
    data = preview.json()
    assert data["supplier"]["iikoSupplierId"] == "SUP-001"
    assert data["items"][0]["iikoProductId"] == "PROD-001"
    assert data["items"][0]["productArticle"] == "SUGAR-001"
    assert data["items"][0]["mappingStatus"] == "ready"



def test_invoice_review_sheet_clears_non_visible_values_on_torg12_continuation_page():
    response = client.post(
        "/api/v1/invoice-review/upload",
        json={
            "raw_text": """
            Страница 2
            Товарная накладная имеет приложение на
            Всего отпущено на сумму
            Окорок "По-тамбовски" к/в в/у
            166 кг
            4,058
            702,00
            2 848,72
            7%
            199,41
            3 048,13
            16 351,45
            ООО "ЛИР", ИНН 3906400288
            """,
            "document_form": "ТОРГ-12",
            "total_sum": 16351.45,
            "items": [
                {
                    "name": "Окорок \"По-тамбовски\" к/в в/у",
                    "quantity": 4.058,
                    "unit": "кг",
                    "price": 702.0,
                    "sum": 2848.72,
                    "vat": "7%",
                    "vat_percent": 7.0,
                    "vat_sum": 199.41,
                }
            ],
        },
    )

    assert response.status_code == 200
    review_id = response.json()["review_id"]

    db = TestingSessionLocal()
    try:
        receiving = db.query(Receiving).filter(Receiving.id == review_id).one()
        receiving.supplier = "41"
        receiving.venue = "41"
        document = receiving.documents[-1]
        document.invoice_number = "41"
        document.invoice_date = "41"
        meta = json.loads(document.recognized_items_json)
        meta["header"].update(
            {
                "document_number": "41",
                "supplier_inn": "3906400288",
                "consignee": "41",
                "recipient": "41",
                "trade_point": "41",
                "warehouse": "41",
                "basis": "41",
                "duplicate_indicator": "41",
            }
        )
        meta["items"][0].update(
            {
                "egais": "41",
                "mercury": "41",
                "honest_sign": "41",
                "iiko_product_id": "41",
                "product_article": "41",
                "amount_unit": "41",
                "acceptance_date": "41",
                "accepted_by": "41",
                "last_delivery_date": "41",
                "last_price": "41",
            }
        )
        document.recognized_items_json = json.dumps(meta, ensure_ascii=False)
        db.commit()
    finally:
        db.close()

    sheet = client.get(f"/api/v1/invoice-review/{review_id}/sheet")
    assert sheet.status_code == 200
    rows = sheet.json()["sheets"]["Накладные"]
    data_row = dict(zip(rows[0], rows[1], strict=True))

    # На странице-продолжении шапки документа нет, поэтому эти поля не заполняем.
    for field in (
        "Индикатор дубля документа",
        "Дата документа",
        "№ Документа",
        "Поставщик",
        "ИНН Поставщика",
        "Грузополучатель",
        "Получатель",
        "Торговая точка",
        "Склад",
        "Основание",
    ):
        assert data_row[field] == ""

    # Пользовательские/ручные/УС-поля не должны заполняться техническими значениями.
    for field in (
        "Госсистемы",
        "Дата приема",
        "Принял, Ф.И.О.",
        "Кол-во в заявке",
        "Цена по прайсу",
        "Последняя дата поставки",
        "Последняя цена",
        "Отклонение от цены прайса",
        "Загрузить в УС",
    ):
        assert data_row[field] == ""
    assert data_row["Товар найден в справочнике"] != "41"
    assert data_row["Статус строки"] != "41"
    assert data_row["Причина ручной корректировки"] != "41"

    assert data_row["ID документа"] == review_id
    assert data_row["Форма документа"] == "ТОРГ-12"
    assert data_row["Наименование товара из документа"] == "Окорок \"По-тамбовски\" к/в в/у"
    assert data_row["Наименование товара в УС"] == "Окорок По-тамбовски"
    assert data_row["Ед.изм."] == "кг"
    assert data_row["Ед.изм. в УС"] == "кг"
    assert data_row["Кол-во в УС"] == 4.058
    assert data_row["Цена в УС"] == 702
    assert data_row["Ставка НДС %"] == "7%"
    assert data_row["Сумма накладной"] == 16351.45
