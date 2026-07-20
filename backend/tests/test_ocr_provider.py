import ssl

import pytest

from app.services import ocr_service
from app.services.ocr_service import OcrProviderError


def test_google_operation_retries_transient_ssl_error(monkeypatch):
    calls = 0
    monkeypatch.setattr(ocr_service.settings, "google_api_retry_attempts", 3)
    monkeypatch.setattr(ocr_service.settings, "google_api_retry_backoff_seconds", 0)

    def operation():
        nonlocal calls
        calls += 1
        if calls < 3:
            raise ssl.SSLError("handshake timed out")
        return "ok"

    assert ocr_service._execute_google_operation("test", operation) == "ok"
    assert calls == 3


def test_google_operation_exposes_typed_error_after_retries(monkeypatch):
    monkeypatch.setattr(ocr_service.settings, "google_api_retry_attempts", 2)
    monkeypatch.setattr(ocr_service.settings, "google_api_retry_backoff_seconds", 0)

    with pytest.raises(OcrProviderError) as exc_info:
        ocr_service._execute_google_operation(
            "export_ocr_text",
            lambda: (_ for _ in ()).throw(ssl.SSLError("handshake timed out")),
        )

    error = exc_info.value
    assert error.operation == "export_ocr_text"
    assert error.attempts == 2
    assert error.retryable is True


class _FakeExportCall:
    def __init__(self, queue):
        self._queue = queue

    def execute(self):
        return self._queue.pop(0)


class _FakeFiles:
    def __init__(self, responses):
        self._queue = list(responses)

    def export(self, fileId, mimeType):  # noqa: N803 - matches googleapiclient signature
        return _FakeExportCall(self._queue)


class _FakeDriveService:
    def __init__(self, responses):
        self._files = _FakeFiles(responses)

    def files(self):
        return self._files


def test_export_ocr_text_retries_past_empty_bom_only_export(monkeypatch):
    """Drive's OCR conversion is async: export() right after create() can return
    a document that is still just the UTF-8 BOM placeholder. The same file
    uploaded three times in production showed this exact pattern (empty, empty,
    then real text), producing different parsed results from identical input.
    """
    monkeypatch.setattr(ocr_service.settings, "google_drive_ocr_export_retry_attempts", 4)
    monkeypatch.setattr(ocr_service.settings, "google_drive_ocr_export_retry_delay_seconds", 0)
    monkeypatch.setattr(ocr_service.settings, "google_drive_ocr_min_text_length", 20)

    real_text = "﻿ООО \"Поставщик\", ИНН 1234567890, Накладная № 1"
    service = _FakeDriveService([b"\xef\xbb\xbf", b"\xef\xbb\xbf", real_text.encode("utf-8")])

    result = ocr_service._export_ocr_text_with_retry(service, "doc-1")

    assert result == real_text.lstrip("﻿")


def test_export_ocr_text_gives_up_after_exhausting_retries(monkeypatch):
    monkeypatch.setattr(ocr_service.settings, "google_drive_ocr_export_retry_attempts", 2)
    monkeypatch.setattr(ocr_service.settings, "google_drive_ocr_export_retry_delay_seconds", 0)
    monkeypatch.setattr(ocr_service.settings, "google_drive_ocr_min_text_length", 20)

    service = _FakeDriveService([b"\xef\xbb\xbf", b"\xef\xbb\xbf", b"still not enough text"])

    result = ocr_service._export_ocr_text_with_retry(service, "doc-1")

    assert result == ""
