from pathlib import Path

from PIL import Image

from app.services.document_image_preparation_service import prepare_document_page


def test_prepare_document_page_preserves_original_and_creates_derivative(tmp_path: Path, monkeypatch):
    source = tmp_path / "invoice.jpg"
    monkeypatch.setattr(
        "app.services.document_image_preparation_service.settings.uploaded_invoices_dir",
        str(tmp_path / "uploads"),
    )
    Image.new("RGB", (600, 900), "white").save(source)
    original = source.read_bytes()

    result = prepare_document_page(str(source))

    prepared_path = Path(result["prepared_path"])
    assert source.read_bytes() == original
    assert prepared_path.exists()
    assert prepared_path.parent == tmp_path / "uploads" / ".prepared"
    assert result["quality"]["width"] >= 1200
    assert any(value.startswith("upscale_") for value in result["transformations"])
    assert "text_coverage_ratio" in result["quality"]
    assert "review_reasons" in result["quality"]


def test_prepare_document_page_skips_non_image(tmp_path: Path):
    source = tmp_path / "invoice.pdf"
    source.write_bytes(b"%PDF")

    result = prepare_document_page(str(source))

    assert result["prepared_path"] is None
    assert result["quality"]["preparation_skipped"] is True


def test_prepare_document_page_flags_low_text_coverage_for_blank_image(tmp_path: Path, monkeypatch):
    source = tmp_path / "blank.jpg"
    monkeypatch.setattr(
        "app.services.document_image_preparation_service.settings.uploaded_invoices_dir",
        str(tmp_path / "uploads"),
    )
    Image.new("RGB", (1200, 1200), "white").save(source)

    result = prepare_document_page(str(source))

    assert result["quality"]["requires_review"] is True
    assert any("мало текстовых" in value.lower() for value in result["quality"]["review_reasons"])
