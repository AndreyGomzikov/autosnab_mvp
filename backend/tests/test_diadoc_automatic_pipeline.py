from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.config import settings
from app.db.session import Base
from app.models.diadoc import DiadocArtifact, DiadocDocument
from app.models.receiving import OrderItemSnapshot, Receiving, ReceivingStatus
from app.services.diadoc_client import DiadocApiError, DiadocBinaryResponse
from app.services.diadoc_sync_service import retry_failed_diadoc_documents, sync_diadoc_documents


XML = '''<?xml version="1.0" encoding="utf-8"?>
<Файл>
  <Документ>
    <СвСчФакт НомерСчФ="77" ДатаСчФ="19.07.2026" />
    <СвПрод><ИдСв><СвЮЛУч НаимОрг="Поставщик" ИННЮЛ="1234567890" /></ИдСв></СвПрод>
    <ОснПер НаимОсн="Заказ" НомОсн="ORDER-42" ДатаОсн="18.07.2026" />
    <ТаблСчФакт>
      <СведТов НаимТов="Молоко" КолТов="2" ЦенаТов="100" СтТовУчНал="200" НаимЕдИзм="шт" />
    </ТаблСчФакт>
    <ВсегоОпл СтТовУчНалВсего="200" />
  </Документ>
</Файл>'''.encode("utf-8")


@pytest.fixture()
def db():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    try:
        yield session
    finally:
        session.close()


class FakeClient:
    def __init__(self, *, fail_content_once: bool = False):
        self.fail_content_once = fail_content_once
        self.content_calls = 0

    def get_last_event(self, **_kwargs):
        return None

    def get_new_events(self, **_kwargs):
        return {"Events": [{"EventId": "evt-1", "IndexKey": "idx-1", "MessageId": "msg-1"}]}

    def get_message(self, **_kwargs):
        return {
            "Entities": [
                {
                    "EntityType": "Attachment",
                    "TypeNamedId": "UniversalTransferDocument",
                    "EntityId": "entity-1",
                    "FileName": "upd.xml",
                }
            ]
        }

    def get_entity_content(self, **_kwargs):
        self.content_calls += 1
        if self.fail_content_once and self.content_calls == 1:
            raise DiadocApiError("temporary content error")
        return DiadocBinaryResponse(XML, "application/xml", "upd.xml")

    def generate_print_form(self, **_kwargs):
        return DiadocBinaryResponse(b"%PDF-1.4 test", "application/pdf", "upd.pdf")


def _configure(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(settings, "diadoc_integration_enabled", True)
    monkeypatch.setattr(settings, "diadoc_access_token", "token")
    monkeypatch.setattr(settings, "diadoc_refresh_token", None)
    monkeypatch.setattr(settings, "diadoc_box_id", "box-1")
    monkeypatch.setattr(settings, "diadoc_documents_dir", str(tmp_path / "diadoc"))
    monkeypatch.setattr(settings, "diadoc_generate_print_form", True)
    monkeypatch.setattr(settings, "diadoc_initial_sync_mode", "oldest")
    monkeypatch.setattr(settings, "diadoc_max_pages_per_sync", 10)
    monkeypatch.setattr(settings, "diadoc_parse_unstructured_attachments", False)
    monkeypatch.setattr(settings, "google_sheets_enabled", False)


def test_full_automatic_xml_pipeline_matches_existing_order(db, monkeypatch, tmp_path):
    _configure(monkeypatch, tmp_path)
    receiving = Receiving(
        request_id="REQ-42",
        order_number="ORDER-42",
        venue="Кафе",
        supplier="Поставщик",
        status=ReceivingStatus.receiving_started,
    )
    db.add(receiving)
    db.flush()
    db.add(
        OrderItemSnapshot(
            receiving_id=receiving.id,
            name="Молоко",
            quantity=2,
            unit="шт",
            price=100,
        )
    )
    db.commit()

    fake = FakeClient()
    monkeypatch.setattr("app.services.diadoc_sync_service.DiadocClient", lambda: fake)

    result = sync_diadoc_documents(db, create_google_sheet=False)

    assert result["documents_processed"] == 1
    assert result["artifacts_downloaded"] == 2
    document = db.query(DiadocDocument).one()
    assert document.review_id == receiving.id
    assert document.status == "processed"
    db.refresh(receiving)
    assert receiving.status == ReceivingStatus.matched_full
    assert len(receiving.items) == 1
    assert len(db.query(DiadocArtifact).all()) == 2
    assert any(item.file_type == "pdf" for item in receiving.documents)


def test_failed_content_is_retried_without_losing_event(db, monkeypatch, tmp_path):
    _configure(monkeypatch, tmp_path)
    monkeypatch.setattr(settings, "diadoc_retry_base_delay_seconds", 0)
    fake = FakeClient(fail_content_once=True)
    monkeypatch.setattr("app.services.diadoc_sync_service.DiadocClient", lambda: fake)

    first = sync_diadoc_documents(db, create_google_sheet=False)
    assert first["documents_failed"] == 1
    document = db.query(DiadocDocument).one()
    assert document.status == "failed"

    retried = retry_failed_diadoc_documents(db, client=fake, create_google_sheet=False)
    assert retried == {"retried": 1, "recovered": 1, "failed": 0}
    db.refresh(document)
    assert document.status == "processed"
    assert document.review_id is not None


class MultiAttachmentClient(FakeClient):
    def get_message(self, **_kwargs):
        return {
            "Entities": [
                {
                    "EntityType": "Attachment",
                    "EntityId": "entity-note",
                    "FileName": "note.txt",
                },
                {
                    "EntityType": "Attachment",
                    "TypeNamedId": "UniversalTransferDocument",
                    "EntityId": "entity-xml",
                    "FileName": "upd.xml",
                },
            ]
        }

    def get_entity_content(self, **kwargs):
        if kwargs["entity_id"] == "entity-xml":
            return DiadocBinaryResponse(XML, "application/xml", "upd.xml")
        return DiadocBinaryResponse(b"attachment", "text/plain", "note.txt")


def test_additional_message_attachments_use_same_review(db, monkeypatch, tmp_path):
    _configure(monkeypatch, tmp_path)
    fake = MultiAttachmentClient()
    monkeypatch.setattr("app.services.diadoc_sync_service.DiadocClient", lambda: fake)

    result = sync_diadoc_documents(db, create_google_sheet=False)

    assert result["documents_discovered"] == 2
    documents = db.query(DiadocDocument).order_by(DiadocDocument.id).all()
    assert len({document.review_id for document in documents}) == 1
    review = db.get(Receiving, documents[0].review_id)
    assert review is not None
    assert any(item.file_type == "txt" for item in review.documents)


def test_google_sheet_failure_is_retried_from_delivery_queue(db, monkeypatch, tmp_path):
    from app.models.diadoc import DiadocDelivery
    from app.services.diadoc_sync_service import retry_diadoc_deliveries

    _configure(monkeypatch, tmp_path)
    monkeypatch.setattr(settings, "google_sheets_enabled", True)
    monkeypatch.setattr(settings, "diadoc_retry_base_delay_seconds", 0)
    calls = {"count": 0}

    def flaky_sheet(*_args, **_kwargs):
        calls["count"] += 1
        if calls["count"] == 1:
            raise RuntimeError("sheet temporarily unavailable")
        return {"spreadsheet_id": "sheet-1", "mode": "created"}

    monkeypatch.setattr(
        "app.services.diadoc_sync_service.create_real_google_sheet_for_review",
        flaky_sheet,
    )
    fake = FakeClient()
    monkeypatch.setattr("app.services.diadoc_sync_service.DiadocClient", lambda: fake)

    first = sync_diadoc_documents(db, create_google_sheet=True)

    document = db.query(DiadocDocument).one()
    assert document.status == "processed"
    sheet_delivery = (
        db.query(DiadocDelivery)
        .filter(DiadocDelivery.delivery_type == "google_sheets")
        .one()
    )
    assert sheet_delivery.status == "failed"
    assert first["deliveries_failed"] == 1

    retried = retry_diadoc_deliveries(db, client=fake)

    db.refresh(sheet_delivery)
    assert retried["recovered"] == 1
    assert sheet_delivery.status == "succeeded"
    assert calls["count"] == 2


class PdfOnlyClient(FakeClient):
    def get_message(self, **_kwargs):
        return {
            "Entities": [
                {
                    "EntityType": "Attachment",
                    "EntityId": "entity-pdf",
                    "FileName": "invoice.pdf",
                }
            ]
        }

    def get_entity_content(self, **_kwargs):
        return DiadocBinaryResponse(b"%PDF-1.4 test", "application/pdf", "invoice.pdf")


def test_pdf_only_document_is_parsed_and_sent_to_verification(db, monkeypatch, tmp_path):
    _configure(monkeypatch, tmp_path)
    monkeypatch.setattr(settings, "diadoc_generate_print_form", False)
    monkeypatch.setattr(settings, "diadoc_parse_unstructured_attachments", True)
    monkeypatch.setattr(
        "app.services.diadoc_sync_service.extract_invoice_document",
        lambda *_args, **_kwargs: {
            "raw_text": "invoice evidence",
            "payload": {
                "supplier": "PDF Supplier",
                "supplier_legal_name": "PDF Supplier",
                "invoice_number": "PDF-77",
                "invoice_date": "2026-07-19",
                "items": [
                    {"name": "Кефир", "quantity": 2, "unit": "шт", "price": 50}
                ],
                "parser_metadata": {},
            },
        },
    )
    fake = PdfOnlyClient()
    monkeypatch.setattr("app.services.diadoc_sync_service.DiadocClient", lambda: fake)

    result = sync_diadoc_documents(db, create_google_sheet=False)

    assert result["documents_processed"] == 1
    document = db.query(DiadocDocument).one()
    assert document.status == "processed"
    assert document.review_id is not None
    review = db.get(Receiving, document.review_id)
    assert review is not None
    assert review.supplier == "PDF Supplier"
    assert len(review.items) == 1


class LateXmlClient(FakeClient):
    def __init__(self):
        super().__init__()
        self.round = 0

    def get_new_events(self, **_kwargs):
        self.round += 1
        return {
            "Events": [
                {
                    "EventId": f"evt-{self.round}",
                    "IndexKey": f"idx-{self.round}",
                    "MessageId": "msg-late",
                }
            ]
        }

    def get_message(self, **_kwargs):
        note = {
            "EntityType": "Attachment",
            "EntityId": "entity-note",
            "FileName": "note.txt",
        }
        if self.round == 1:
            return {"Entities": [note]}
        return {
            "Entities": [
                note,
                {
                    "EntityType": "Attachment",
                    "TypeNamedId": "UniversalTransferDocument",
                    "EntityId": "entity-xml",
                    "FileName": "upd.xml",
                },
            ]
        }

    def get_entity_content(self, **kwargs):
        if kwargs["entity_id"] == "entity-xml":
            return DiadocBinaryResponse(XML, "application/xml", "upd.xml")
        return DiadocBinaryResponse(b"attachment", "text/plain", "note.txt")


def test_earlier_downloaded_attachment_is_linked_when_xml_arrives(db, monkeypatch, tmp_path):
    _configure(monkeypatch, tmp_path)
    fake = LateXmlClient()
    monkeypatch.setattr("app.services.diadoc_sync_service.DiadocClient", lambda: fake)

    first = sync_diadoc_documents(db, create_google_sheet=False)
    note = db.query(DiadocDocument).one()
    assert first["documents_downloaded"] == 1
    assert note.review_id is None

    second = sync_diadoc_documents(db, create_google_sheet=False)

    db.refresh(note)
    assert second["documents_processed"] == 1
    assert note.review_id is not None
    review = db.get(Receiving, note.review_id)
    assert review is not None
    assert any(item.file_type == "txt" for item in review.documents)


class ServiceXmlClient(FakeClient):
    def get_message(self, **_kwargs):
        return {
            "Entities": [
                {
                    "EntityType": "Attachment",
                    "AttachmentType": "InvoiceConfirmation",
                    "EntityId": "entity-confirmation",
                    "FileName": "confirmation.xml",
                },
                {
                    "EntityType": "Attachment",
                    "TypeNamedId": "UniversalTransferDocument",
                    "EntityId": "entity-upd",
                    "FileName": "upd.xml",
                },
            ]
        }

    def get_entity_content(self, **kwargs):
        if kwargs["entity_id"] == "entity-upd":
            return DiadocBinaryResponse(
                XML,
                "application/xml",
                "upd.xml",
            )
        return DiadocBinaryResponse(
            b"<?xml version='1.0'?><Confirmation/>",
            "application/xml",
            "confirmation.xml",
        )


def test_service_xml_is_stored_without_creating_false_invoice(
    db,
    monkeypatch,
    tmp_path,
):
    _configure(monkeypatch, tmp_path)
    fake = ServiceXmlClient()
    monkeypatch.setattr(
        "app.services.diadoc_sync_service.DiadocClient",
        lambda: fake,
    )

    result = sync_diadoc_documents(
        db,
        create_google_sheet=False,
    )

    documents = (
        db.query(DiadocDocument)
        .order_by(DiadocDocument.entity_id)
        .all()
    )
    assert result["documents_processed"] == 1
    assert result["documents_downloaded"] == 1
    assert db.query(Receiving).count() == 1
    confirmation = next(
        item
        for item in documents
        if item.entity_id == "entity-confirmation"
    )
    assert confirmation.status == "downloaded"
    assert confirmation.review_id is not None


class PagedClient(FakeClient):
    def __init__(self):
        super().__init__()
        self.requested_cursors = []
        self.current_message = ""

    def get_new_events(self, **kwargs):
        cursor = kwargs.get("after_index_key")
        self.requested_cursors.append(cursor)
        if cursor is None:
            return {
                "Events": [
                    {
                        "EventId": "evt-page-1",
                        "IndexKey": "idx-page-1",
                        "MessageId": "msg-page-1",
                    }
                ]
            }
        if cursor == "idx-page-1":
            return {
                "Events": [
                    {
                        "EventId": "evt-page-2",
                        "IndexKey": "idx-page-2",
                        "MessageId": "msg-page-2",
                    }
                ]
            }
        return {"Events": []}

    def get_message(self, **kwargs):
        self.current_message = kwargs["message_id"]
        return {
            "Entities": [
                {
                    "EntityType": "Attachment",
                    "TypeNamedId": "UniversalTransferDocument",
                    "EntityId": f"entity-{self.current_message}",
                    "FileName": f"{self.current_message}.xml",
                }
            ]
        }

    def get_entity_content(self, **kwargs):
        return DiadocBinaryResponse(
            XML,
            "application/xml",
            f"{kwargs['entity_id']}.xml",
        )


def test_sync_reads_multiple_event_pages_in_one_cycle(
    db,
    monkeypatch,
    tmp_path,
):
    from app.models.diadoc import DiadocSyncState

    _configure(monkeypatch, tmp_path)
    monkeypatch.setattr(settings, "diadoc_sync_limit", 1)
    monkeypatch.setattr(settings, "diadoc_max_pages_per_sync", 3)
    fake = PagedClient()
    monkeypatch.setattr(
        "app.services.diadoc_sync_service.DiadocClient",
        lambda: fake,
    )

    result = sync_diadoc_documents(
        db,
        create_google_sheet=False,
    )

    state = db.query(DiadocSyncState).one()
    assert result["pages_received"] == 3
    assert result["events_received"] == 2
    assert result["documents_processed"] == 2
    assert state.after_index_key == "idx-page-2"
    assert fake.requested_cursors == [
        None,
        "idx-page-1",
        "idx-page-2",
    ]


class LatestCursorClient(FakeClient):
    def __init__(self):
        super().__init__()
        self.cursor = None

    def get_last_event(self, **_kwargs):
        return {"IndexKey": "idx-existing"}

    def get_new_events(self, **kwargs):
        self.cursor = kwargs.get("after_index_key")
        return {"Events": []}


def test_latest_initial_mode_skips_existing_history(
    db,
    monkeypatch,
    tmp_path,
):
    from app.models.diadoc import DiadocSyncState

    _configure(monkeypatch, tmp_path)
    monkeypatch.setattr(
        settings,
        "diadoc_initial_sync_mode",
        "latest",
    )
    fake = LatestCursorClient()
    monkeypatch.setattr(
        "app.services.diadoc_sync_service.DiadocClient",
        lambda: fake,
    )

    result = sync_diadoc_documents(
        db,
        create_google_sheet=False,
    )

    state = db.query(DiadocSyncState).one()
    assert result["events_received"] == 0
    assert fake.cursor == "idx-existing"
    assert state.after_index_key == "idx-existing"


class OversizedClient(FakeClient):
    def get_entity_content(self, **_kwargs):
        return DiadocBinaryResponse(
            b"x" * 32,
            "application/octet-stream",
            "large.bin",
        )


def test_oversized_attachment_goes_directly_to_dead_letter(
    db,
    monkeypatch,
    tmp_path,
):
    _configure(monkeypatch, tmp_path)
    monkeypatch.setattr(
        settings,
        "diadoc_max_attachment_bytes",
        8,
    )
    fake = OversizedClient()
    monkeypatch.setattr(
        "app.services.diadoc_sync_service.DiadocClient",
        lambda: fake,
    )

    result = sync_diadoc_documents(
        db,
        create_google_sheet=False,
    )

    document = db.query(DiadocDocument).one()
    assert result["documents_failed"] == 1
    assert document.status == "dead_letter"
    assert "размер" in (document.error_text or "")
