"""iiko reference synchronization and automatic invoice field mapping.

This module fills technical iiko fields for MVP-4 automatically when real iiko
credentials are configured. If iiko is not configured, it keeps the user-facing
invoice data and marks missing technical fields for review instead of pretending
that mapping succeeded.
"""

from __future__ import annotations

import json
import re
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Any

from sqlalchemy.orm import Session
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from app.config import settings
from app.services.iiko_incoming_invoice_service import IikoConfigurationError, IikoRequestError
from app.services.reference_catalog_service import (
    find_local_reference,
    load_local_reference_entries,
    upsert_reference_entry,
)

_CACHE_TTL_SECONDS = 300
_CACHE: dict[str, Any] = {"loaded_at": 0, "context": None, "error": None}


@dataclass
class MatchResult:
    value: str | None
    name: str | None
    confidence: float
    status: str
    reason: str
    extra: dict[str, Any]


def auto_fill_iiko_fields(
    header: dict[str, Any],
    items: list[dict[str, Any]],
    *,
    supplier_name: str | None,
    venue: str | None,
    db: Session | None = None,
) -> dict[str, Any]:
    """Return header/items enriched with iiko ids/articles/units/tax fields.

    The function is safe for local MVP runs: if iiko is disabled or unavailable,
    it leaves the original values and writes mapping statuses/errors so the
    user sees what still needs attention in Google Sheets.
    """
    enriched_header = dict(header or {})
    enriched_items = [dict(item) for item in items]
    notes: list[str] = []
    discovered_references: list[dict[str, Any]] = []
    local_suppliers = load_local_reference_entries(db, "supplier") if db is not None else []
    local_products = load_local_reference_entries(db, "product", venue) if db is not None else []

    context_result = get_iiko_reference_context()
    context = context_result.get("context") or {}
    context_status = context_result.get("status") or "error"
    if context_status != "ready":
        message = context_result.get("message") or "Справочники iiko недоступны; технические поля требуют проверки"
        notes.append(message)

    supplier_match, supplier_source = _match_with_local_first(
        supplier_name,
        local_suppliers,
        context.get("suppliers", []),
        _match_supplier,
        external_status=context_status,
    )
    if enriched_header.get("iiko_supplier_id"):
        supplier_match = MatchResult(
            str(enriched_header["iiko_supplier_id"]),
            enriched_header.get("iiko_supplier_name") or supplier_name,
            1.0,
            "ready",
            "iiko supplier id передан явно",
            {},
        )
        supplier_source = "provided"
        supplier_reference_status = "matched"
    else:
        supplier_reference_status = _reference_status(supplier_match, supplier_source)
    if (
        not enriched_header.get("iiko_supplier_id")
        and supplier_match.value
        and supplier_reference_status == "matched"
    ):
        enriched_header["iiko_supplier_id"] = supplier_match.value
    enriched_header["iiko_supplier_name"] = supplier_match.name or supplier_name
    enriched_header["iiko_supplier_match_confidence"] = supplier_match.confidence
    enriched_header["iiko_supplier_reference_source"] = supplier_source
    enriched_header["iiko_supplier_reference_status"] = supplier_reference_status
    if db is not None and supplier_name:
        supplier_entry, supplier_created = upsert_reference_entry(
            db,
            kind="supplier",
            venue=None,
            raw_name=supplier_name,
            external_id=supplier_match.value,
            external_name=supplier_match.name,
            unit=None,
            status=supplier_reference_status,
            confidence=supplier_match.confidence,
            source=supplier_source,
            candidates=_candidate_payloads(supplier_name, context.get("suppliers", []), ["name", "legalName", "code", "taxpayerIdNumber"]),
        )
        if supplier_created:
            discovered_references.append(_reference_entry_payload(supplier_entry))

    store_match = _match_store(
        enriched_header.get("iiko_default_store_id") or enriched_header.get("iiko_organization") or venue,
        context.get("stores", []),
    )
    if not enriched_header.get("iiko_default_store_id") and store_match.value:
        enriched_header["iiko_default_store_id"] = store_match.value
    enriched_header["iiko_default_store_name"] = store_match.name or enriched_header.get("iiko_default_store_id")
    enriched_header["iiko_default_store_match_confidence"] = store_match.confidence

    header_errors = []
    if not enriched_header.get("iiko_supplier_id"):
        header_errors.append(supplier_match.reason)
    if not enriched_header.get("iiko_default_store_id"):
        header_errors.append(store_match.reason)
    enriched_header["iiko_mapping_status"] = "ready" if not header_errors else "needs_review"
    enriched_header["iiko_mapping_error"] = "; ".join(err for err in header_errors if err)

    for item in enriched_items:
        discovered = _auto_fill_item(
            item,
            enriched_header,
            context,
            local_products=local_products,
            venue=venue,
            db=db,
            context_status=context_status,
        )
        if discovered is not None:
            discovered_references.append(discovered)
            local_products.append(discovered)

    return {
        "header": enriched_header,
        "items": enriched_items,
        "notes": notes,
        "context_status": context_status,
        "discovered_references": discovered_references,
    }


def get_iiko_reference_context(force_refresh: bool = False) -> dict[str, Any]:
    """Load iiko reference context: suppliers, products, stores, units, taxes."""
    if not settings.iiko_integration_enabled:
        return {"status": "disabled", "message": "IIKO_INTEGRATION_ENABLED=false; автосопоставление со справочниками iiko отключено"}
    if not settings.iiko_base_url:
        return {"status": "not_configured", "message": "Не указан IIKO_BASE_URL"}

    now = time.time()
    if not force_refresh and _CACHE["context"] is not None and now - _CACHE["loaded_at"] < _CACHE_TTL_SECONDS:
        return {"status": "ready", "context": _CACHE["context"], "cached": True}

    try:
        token = settings.iiko_token or _authorize()
        context = {
            "suppliers": _normalize_entities(_request_iiko("/resto/api/suppliers", token), kind="supplier"),
            "products": _normalize_entities(_request_iiko("/resto/api/v2/entities/products/list", token, {"includeDeleted": "false"}), kind="product"),
            "stores": _normalize_entities(_request_iiko("/resto/api/v2/entities/list", token, {"rootType": "Account", "includeDeleted": "false"}), kind="store"),
            "units": _normalize_entities(_request_iiko("/resto/api/v2/entities/list", token, {"rootType": "MeasureUnit", "includeDeleted": "false"}), kind="unit"),
            "taxes": _normalize_entities(_request_iiko("/resto/api/v2/entities/list", token, {"rootType": "TaxCategory", "includeDeleted": "false"}), kind="tax"),
        }
        _CACHE.update({"loaded_at": now, "context": context, "error": None})
        return {"status": "ready", "context": context, "cached": False}
    except Exception as exc:  # noqa: BLE001 - external iiko errors must be visible in Sheets
        _CACHE.update({"loaded_at": now, "context": None, "error": str(exc)})
        return {"status": "error", "message": f"Не удалось загрузить справочники iiko: {exc}"}


def invalidate_iiko_reference_cache() -> None:
    _CACHE.update({"loaded_at": 0, "context": None, "error": None})


def _auto_fill_item(
    item: dict[str, Any],
    header: dict[str, Any],
    context: dict[str, Any],
    *,
    local_products: list[dict[str, Any]],
    venue: str | None,
    db: Session | None,
    context_status: str,
) -> dict[str, Any] | None:
    item_name = item.get("name") or ""
    product_match, product_source = _match_with_local_first(
        item_name,
        local_products,
        context.get("products", []),
        _match_product,
        external_status=context_status,
    )
    if item.get("iiko_product_id") or item.get("product_article"):
        product_match = MatchResult(
            item.get("iiko_product_id") or item.get("product_article"),
            item.get("iiko_product_name") or item_name,
            1.0,
            "ready",
            "iiko product id/article передан явно",
            {"num": item.get("product_article")},
        )
        product_source = "provided"
        product_reference_status = "matched"
    else:
        product_reference_status = _reference_status(product_match, product_source)
    if (
        not item.get("iiko_product_id")
        and product_match.value
        and product_reference_status == "matched"
    ):
        item["iiko_product_id"] = product_match.value
    if not item.get("product_article") and product_reference_status == "matched":
        article = product_match.extra.get("num") or product_match.extra.get("code")
        if article:
            item["product_article"] = article
    item["iiko_product_name"] = product_match.name or item_name
    item["iiko_product_match_confidence"] = product_match.confidence
    item["reference_source"] = product_source
    item["reference_status"] = product_reference_status
    item["reference_candidates"] = _candidate_payloads(
        item_name,
        context.get("products", []),
        ["name", "num", "code", "description"],
    )

    unit_match = _match_unit(item.get("unit") or item.get("amount_unit"), context.get("units", []))
    if not item.get("amount_unit"):
        item["amount_unit"] = unit_match.value or unit_match.name or item.get("unit") or "шт"
    item["iiko_unit_name"] = unit_match.name or item.get("unit")
    item["iiko_unit_match_confidence"] = unit_match.confidence

    if not item.get("store_id") and header.get("iiko_default_store_id"):
        item["store_id"] = header["iiko_default_store_id"]

    if item.get("vat_percent") is None:
        tax_percent = _tax_percent_from_product(product_match.extra, context.get("taxes", []))
        if tax_percent is not None:
            item["vat_percent"] = tax_percent
    if item.get("vat_sum") is None and item.get("vat_percent") is not None:
        try:
            item_sum = float(item.get("sum") if item.get("sum") is not None else float(item.get("quantity") or 0) * float(item.get("price") or 0))
            vat_percent = float(item.get("vat_percent") or 0)
            item["vat_sum"] = round(item_sum * vat_percent / (100 + vat_percent), 2) if vat_percent > 0 else 0
        except (TypeError, ValueError):
            pass

    errors = []
    if product_reference_status != "matched" or not (item.get("iiko_product_id") or item.get("product_article")):
        errors.append(product_match.reason)
    if not item.get("amount_unit"):
        errors.append(unit_match.reason)
    if not item.get("store_id"):
        errors.append("не определен склад/defaultStore для позиции")
    item["mapping_status"] = "ready" if not errors else "needs_review"
    item["mapping_error"] = "; ".join(err for err in errors if err)
    if db is None or not item_name:
        return None
    entry, created = upsert_reference_entry(
        db,
        kind="product",
        venue=venue,
        raw_name=item_name,
        external_id=product_match.value,
        external_name=product_match.name,
        unit=item.get("amount_unit") or item.get("unit"),
        status=product_reference_status,
        confidence=product_match.confidence,
        source=product_source,
        candidates=item["reference_candidates"],
    )
    return _reference_entry_payload(entry) if created else None


def _mark_item_if_missing(item: dict[str, Any], status: str, message: str) -> None:
    item.setdefault("amount_unit", item.get("unit") or "шт")
    item.setdefault("mapping_status", status)
    if not (item.get("iiko_product_id") or item.get("product_article")):
        item.setdefault("mapping_error", message)


def _match_supplier(name: str | None, suppliers: list[dict[str, Any]]) -> MatchResult:
    return _best_match(name, suppliers, ["name", "legalName", "code", "taxpayerIdNumber"], "поставщик iiko не найден")


def _match_store(name: str | None, stores: list[dict[str, Any]]) -> MatchResult:
    if not name and len(stores) == 1:
        store = stores[0]
        return MatchResult(store.get("id"), store.get("name"), 0.6, "auto_single", "выбран единственный склад iiko", store)
    return _best_match(name, stores, ["name", "code"], "склад/defaultStore iiko не найден")


def _match_product(name: str | None, products: list[dict[str, Any]]) -> MatchResult:
    return _best_match(name, products, ["name", "num", "code", "description"], "товар iiko не найден")


def _match_unit(name: str | None, units: list[dict[str, Any]]) -> MatchResult:
    if not units and name:
        return MatchResult(str(name), str(name), 0.4, "fallback", "единицы iiko недоступны; оставлена OCR-единица", {})
    return _best_match(name, units, ["name", "code", "shortName"], "единица измерения iiko не найдена")


def _best_match(query: str | None, entities: list[dict[str, Any]], fields: list[str], not_found_reason: str) -> MatchResult:
    query_norm = _normalize_text(query)
    if not query_norm:
        return MatchResult(None, None, 0.0, "needs_review", not_found_reason, {})
    best_entity = None
    best_score = 0.0
    for entity in entities:
        for field in fields:
            value = entity.get(field)
            score = _similarity(query_norm, _normalize_text(value))
            if score > best_score:
                best_score = score
                best_entity = entity
    if best_entity and best_score >= settings.iiko_mapping_min_confidence:
        return MatchResult(best_entity.get("id") or best_entity.get("code") or best_entity.get("num"), best_entity.get("name"), best_score, "ready", "сопоставлено автоматически", best_entity)
    if best_entity and best_score >= settings.iiko_mapping_review_confidence:
        return MatchResult(best_entity.get("id") or best_entity.get("code") or best_entity.get("num"), best_entity.get("name"), best_score, "needs_review", "низкая уверенность автоматического сопоставления", best_entity)
    return MatchResult(None, best_entity.get("name") if best_entity else None, best_score, "needs_review", not_found_reason, best_entity or {})


def _match_with_local_first(
    query: str | None,
    local_entries: list[dict[str, Any]],
    external_entities: list[dict[str, Any]],
    external_matcher,
    *,
    external_status: str = "ready",
) -> tuple[MatchResult, str]:
    local = find_local_reference(query, local_entries, settings.iiko_mapping_min_confidence)
    if local is not None and local.get("status") == "matched":
        return (
            MatchResult(
                local.get("external_id"),
                local.get("external_name") or local.get("raw_name"),
                float(local.get("match_confidence") or local.get("confidence") or 1.0),
                "ready",
                "сопоставлено по локальному справочнику MVP",
                local,
            ),
            "local",
        )
    if external_status != "ready":
        return (
            MatchResult(
                None,
                None,
                0.0,
                "needs_review",
                "справочник учетной системы недоступен",
                {},
            ),
            external_status,
        )
    return external_matcher(query, external_entities), "iiko"


def _reference_status(match: MatchResult, source: str = "iiko") -> str:
    if match.value and match.confidence >= settings.iiko_mapping_min_confidence:
        return "matched"
    if source not in {"local", "iiko"}:
        return "needs_review"
    if match.confidence >= settings.iiko_mapping_review_confidence:
        return "needs_review"
    return "new"


def _candidate_payloads(
    query: str | None,
    entities: list[dict[str, Any]],
    fields: list[str],
    limit: int = 3,
) -> list[dict[str, Any]]:
    query_norm = _normalize_text(query)
    ranked: list[tuple[float, dict[str, Any]]] = []
    for entity in entities:
        score = max((_similarity(query_norm, _normalize_text(entity.get(field))) for field in fields), default=0.0)
        if score > 0:
            ranked.append((score, entity))
    ranked.sort(key=lambda pair: pair[0], reverse=True)
    return [
        {
            "external_id": entity.get("id") or entity.get("code") or entity.get("num"),
            "name": entity.get("name"),
            "confidence": round(score, 4),
        }
        for score, entity in ranked[:limit]
    ]


def _reference_entry_payload(entry) -> dict[str, Any]:
    try:
        candidates = json.loads(entry.candidates_json or "[]")
    except json.JSONDecodeError:
        candidates = []
    return {
        "kind": entry.kind,
        "venue": entry.venue,
        "raw_name": entry.raw_name,
        "normalized_name": entry.normalized_name,
        "external_id": entry.external_id,
        "external_name": entry.external_name,
        "unit": entry.unit,
        "status": entry.status,
        "confidence": entry.confidence,
        "source": entry.source,
        "candidates": candidates,
    }


def _similarity(left: str, right: str) -> float:
    if not left or not right:
        return 0.0
    if left == right:
        return 1.0
    if left in right or right in left:
        shorter = min(len(left), len(right))
        longer = max(len(left), len(right))
        return max(0.75, shorter / longer)
    return SequenceMatcher(None, left, right).ratio()


def _normalize_text(value: Any) -> str:
    if value is None:
        return ""
    text = str(value).lower().replace("ё", "е")
    text = re.sub(r"[^a-zа-я0-9]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _tax_percent_from_product(product: dict[str, Any], taxes: list[dict[str, Any]]) -> float | None:
    tax_id = product.get("taxCategory") or product.get("tax_category")
    if not tax_id:
        return None
    for tax in taxes:
        if str(tax.get("id")) == str(tax_id):
            value = tax.get("vatPercent") or tax.get("percent") or tax.get("taxPercent")
            try:
                return float(value) if value is not None else None
            except (TypeError, ValueError):
                return None
    return None


def _authorize() -> str:
    if settings.iiko_token:
        return settings.iiko_token
    if not settings.iiko_login or not settings.iiko_password_sha1:
        raise IikoConfigurationError("Для загрузки справочников iiko нужны IIKO_LOGIN и IIKO_PASSWORD_SHA1 или IIKO_TOKEN")
    url = f"{settings.iiko_base_url.rstrip('/')}/resto/api/auth?{urlencode({'login': settings.iiko_login, 'pass': settings.iiko_password_sha1})}"
    try:
        with urlopen(url, timeout=settings.iiko_timeout_seconds) as response:  # noqa: S310 - configured iiko host
            return response.read().decode("utf-8", errors="replace").strip()
    except HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise IikoRequestError(f"iiko auth HTTP {exc.code}: {body}") from exc
    except URLError as exc:
        raise IikoRequestError(f"Ошибка авторизации iiko: {exc.reason}") from exc


def _request_iiko(path: str, token: str, params: dict[str, str] | None = None) -> Any:
    query = {"key": token}
    if params:
        query.update(params)
    url = f"{settings.iiko_base_url.rstrip('/')}{path}?{urlencode(query)}"
    request = Request(url, method="GET")
    try:
        with urlopen(request, timeout=settings.iiko_timeout_seconds) as response:  # noqa: S310 - configured iiko host
            body = response.read().decode("utf-8", errors="replace")
            return _parse_body(body)
    except HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise IikoRequestError(f"iiko HTTP {exc.code} for {path}: {body}") from exc
    except URLError as exc:
        raise IikoRequestError(f"Ошибка соединения с iiko для {path}: {exc.reason}") from exc


def _parse_body(body: str) -> Any:
    body = body.strip()
    if not body:
        return []
    try:
        return json.loads(body)
    except json.JSONDecodeError:
        return _xml_to_entities(body)


def _xml_to_entities(body: str) -> list[dict[str, Any]]:
    try:
        root = ET.fromstring(body)
    except ET.ParseError:
        return []
    result = []
    for element in root.iter():
        children = list(element)
        if not children:
            continue
        entity = {child.tag.split("}", 1)[-1]: child.text for child in children if child.text is not None}
        if entity.get("id") or entity.get("name") or entity.get("code"):
            result.append(entity)
    return result


def _normalize_entities(data: Any, kind: str) -> list[dict[str, Any]]:
    raw_items = data if isinstance(data, list) else data.get("items", []) if isinstance(data, dict) else []
    entities = []
    for raw in raw_items:
        if not isinstance(raw, dict):
            continue
        entity = dict(raw)
        entity["kind"] = kind
        if "name" not in entity:
            entity["name"] = entity.get("displayName") or entity.get("legalName") or entity.get("fullName") or entity.get("code")
        entities.append(entity)
    return entities
