import os
import sys
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ["DATABASE_URL"] = "sqlite:///:memory:"

from app.config import settings  # noqa: E402
from app.db.session import Base  # noqa: E402
from app.models.reference_catalog import ReferenceCatalogEntry  # noqa: E402
from app.services import iiko_reference_mapping_service  # noqa: E402
from app.services.google_sheets_service import sync_incremental_reference_catalogs  # noqa: E402


def _session():
    engine = create_engine(
        "sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine)()


def _context(products=None, suppliers=None):
    return {
        "status": "ready",
        "context": {
            "suppliers": suppliers or [],
            "stores": [{"id": "STORE-1", "name": "Кафе"}],
            "products": products or [],
            "units": [{"id": "л", "name": "л", "code": "л"}],
            "taxes": [],
        },
    }


def test_first_invoice_matches_iiko_and_persists_local_product(monkeypatch):
    db = _session()
    monkeypatch.setattr(
        iiko_reference_mapping_service,
        "get_iiko_reference_context",
        lambda force_refresh=False: _context(
            products=[{"id": "P-1", "name": "Вода Красный Ключ газ 1.5 л", "num": "WATER-1"}]
        ),
    )
    result = iiko_reference_mapping_service.auto_fill_iiko_fields(
        {},
        [{"name": "Вода Красный Ключ газ 1.5 л", "quantity": 2, "unit": "л"}],
        supplier_name=None,
        venue="Кафе",
        db=db,
    )
    item = result["items"][0]
    assert item["reference_source"] == "iiko"
    assert item["reference_status"] == "matched"
    assert item["iiko_product_id"] == "P-1"
    entry = db.query(ReferenceCatalogEntry).filter_by(kind="product").one()
    assert entry.external_id == "P-1"
    assert entry.status == "matched"


def test_next_invoice_uses_local_mapping_before_iiko(monkeypatch):
    db = _session()
    db.add(
        ReferenceCatalogEntry(
            kind="product",
            venue="Кафе",
            raw_name="Вода Красный Ключ газ 1.5 л",
            normalized_name="вода красный ключ газ 1 5 л",
            external_id="P-LOCAL",
            external_name="Вода Красный Ключ газ 1.5 л",
            unit="л",
            status="matched",
            confidence=0.99,
            source="iiko",
        )
    )
    db.commit()
    monkeypatch.setattr(
        iiko_reference_mapping_service,
        "get_iiko_reference_context",
        lambda force_refresh=False: _context(products=[]),
    )
    result = iiko_reference_mapping_service.auto_fill_iiko_fields(
        {},
        [{"name": "Вода Красный Ключ газ 1.5 л", "quantity": 1, "unit": "л"}],
        supplier_name=None,
        venue="Кафе",
        db=db,
    )
    item = result["items"][0]
    assert item["reference_source"] == "local"
    assert item["reference_status"] == "matched"
    assert item["iiko_product_id"] == "P-LOCAL"


def test_low_confidence_product_is_saved_as_new_without_iiko_id(monkeypatch):
    db = _session()
    monkeypatch.setattr(
        iiko_reference_mapping_service,
        "get_iiko_reference_context",
        lambda force_refresh=False: _context(
            products=[{"id": "P-1", "name": "Сахар ванильный"}]
        ),
    )
    result = iiko_reference_mapping_service.auto_fill_iiko_fields(
        {},
        [{"name": "Экзотический новый соус манго", "quantity": 1, "unit": "л"}],
        supplier_name=None,
        venue="Кафе",
        db=db,
    )
    item = result["items"][0]
    assert item["reference_status"] == "new"
    assert not item.get("iiko_product_id")
    entry = db.query(ReferenceCatalogEntry).filter_by(kind="product").one()
    assert entry.status == "new"
    assert entry.external_id is None


def test_medium_confidence_product_requires_review_and_keeps_candidates(monkeypatch):
    db = _session()
    old_threshold = settings.iiko_mapping_min_confidence
    settings.iiko_mapping_min_confidence = 0.9
    monkeypatch.setattr(
        iiko_reference_mapping_service,
        "get_iiko_reference_context",
        lambda force_refresh=False: _context(
            products=[{"id": "P-1", "name": "Вода Красный Ключ газ 1.5 л"}]
        ),
    )
    try:
        result = iiko_reference_mapping_service.auto_fill_iiko_fields(
            {},
            [{"name": "Красный Ключ вода газ 1.5", "quantity": 1, "unit": "л"}],
            supplier_name=None,
            venue="Кафе",
            db=db,
        )
    finally:
        settings.iiko_mapping_min_confidence = old_threshold
    item = result["items"][0]
    assert item["reference_status"] == "needs_review"
    assert not item.get("iiko_product_id")
    assert item["reference_candidates"][0]["external_id"] == "P-1"


def test_local_mapping_is_used_when_iiko_is_disabled(monkeypatch):
    db = _session()
    db.add(
        ReferenceCatalogEntry(
            kind="product",
            venue="Кафе",
            raw_name="Кефир",
            normalized_name="кефир",
            external_id="P-KEFIR",
            external_name="Кефир",
            unit="л",
            status="matched",
            confidence=1.0,
            source="local_sheet",
        )
    )
    db.commit()
    monkeypatch.setattr(
        iiko_reference_mapping_service,
        "get_iiko_reference_context",
        lambda force_refresh=False: {"status": "disabled", "message": "iiko off"},
    )
    result = iiko_reference_mapping_service.auto_fill_iiko_fields(
        {},
        [{"name": "Кефир", "quantity": 1, "unit": "л"}],
        supplier_name=None,
        venue="Кафе",
        db=db,
    )
    item = result["items"][0]
    assert item["reference_source"] == "local"
    assert item["reference_status"] == "matched"
    assert item["iiko_product_id"] == "P-KEFIR"


def test_unavailable_iiko_does_not_mark_unknown_product_as_new(monkeypatch):
    db = _session()
    monkeypatch.setattr(
        iiko_reference_mapping_service,
        "get_iiko_reference_context",
        lambda force_refresh=False: {"status": "error", "message": "iiko unavailable"},
    )
    result = iiko_reference_mapping_service.auto_fill_iiko_fields(
        {},
        [{"name": "Неизвестный товар", "quantity": 1, "unit": "шт"}],
        supplier_name=None,
        venue="Кафе",
        db=db,
    )
    item = result["items"][0]
    assert item["reference_status"] == "needs_review"
    entry = db.query(ReferenceCatalogEntry).filter_by(kind="product").one()
    assert entry.status == "needs_review"
    assert entry.source == "error"


class _Execute:
    def __init__(self, payload):
        self.payload = payload

    def execute(self):
        return self.payload


class _Values:
    def __init__(self):
        self.appended = []
        self.updated = []

    def get(self, **kwargs):
        if "Товары" in kwargs["range"]:
            return _Execute({"values": [["Наименование", "Код", "Ед. изм."]]})
        return _Execute({"values": [["Поставщик", "Код", "Статус"]]})

    def update(self, **kwargs):
        self.updated.append(kwargs)
        return _Execute({})

    def append(self, **kwargs):
        self.appended.append(kwargs)
        return _Execute({})


class _Spreadsheets:
    def __init__(self, values):
        self._values = values

    def values(self):
        return self._values


class _Service:
    def __init__(self, values):
        self._spreadsheets = _Spreadsheets(values)

    def spreadsheets(self):
        return self._spreadsheets


def test_new_references_are_appended_to_existing_product_and_supplier_tabs(monkeypatch):
    values = _Values()
    old_enabled = settings.google_sheets_enabled
    old_id = settings.google_target_spreadsheet_id
    settings.google_sheets_enabled = True
    settings.google_target_spreadsheet_id = "sheet-id"
    monkeypatch.setattr(
        "app.services.google_sheets_service._build_google_services",
        lambda: (_Service(values), None),
    )
    entries = [
        {
            "kind": "product",
            "raw_name": "Новый товар",
            "external_name": "Новый товар",
            "external_id": "P-NEW",
            "unit": "шт",
            "status": "matched",
            "confidence": 0.97,
            "source": "iiko",
        },
        {
            "kind": "supplier",
            "raw_name": "ООО Новый поставщик",
            "external_name": "ООО Новый поставщик",
            "external_id": None,
            "status": "new",
            "confidence": 0.2,
            "source": "iiko",
        },
    ]
    try:
        result = sync_incremental_reference_catalogs(entries)
    finally:
        settings.google_sheets_enabled = old_enabled
        settings.google_target_spreadsheet_id = old_id
    assert result == {"products": 1, "suppliers": 1}
    assert len(values.appended) == 2
    product_row = values.appended[0]["body"]["values"][0]
    assert product_row == [
        "Новый товар",
        "P-NEW",
        "шт",
        "matched",
        0.97,
        "iiko",
        "Новый товар",
    ]
