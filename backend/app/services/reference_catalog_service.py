from __future__ import annotations

import json
import re
from datetime import datetime
from difflib import SequenceMatcher
from typing import Any

from sqlalchemy.orm import Session

from app.models.reference_catalog import ReferenceCatalogEntry



def normalize_reference_name(value: Any) -> str:
    text = str(value or "").lower().replace("ё", "е")
    text = re.sub(r"[^a-zа-я0-9]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def load_local_reference_entries(db: Session, kind: str, venue: str | None = None) -> list[dict[str, Any]]:
    query = db.query(ReferenceCatalogEntry).filter(ReferenceCatalogEntry.kind == kind)
    venue_value = str(venue or "")
    if kind == "product":
        query = query.filter(ReferenceCatalogEntry.venue.in_([venue_value, ""]))
    return [_entry_payload(entry) for entry in query.order_by(ReferenceCatalogEntry.id.desc()).all()]


def find_local_reference(
    query: str | None,
    entries: list[dict[str, Any]],
    min_confidence: float,
) -> dict[str, Any] | None:
    query_norm = normalize_reference_name(query)
    if not query_norm:
        return None
    best = None
    best_score = 0.0
    for entry in entries:
        target = entry.get("normalized_name") or normalize_reference_name(entry.get("raw_name"))
        score = _similarity(query_norm, target)
        if score > best_score:
            best = entry
            best_score = score
    if best is None or best_score < min_confidence:
        return None
    return {**best, "match_confidence": best_score}


def upsert_reference_entry(
    db: Session,
    *,
    kind: str,
    venue: str | None,
    raw_name: str,
    external_id: str | None,
    external_name: str | None,
    unit: str | None,
    status: str,
    confidence: float,
    source: str,
    candidates: list[dict[str, Any]] | None = None,
) -> tuple[ReferenceCatalogEntry, bool]:
    normalized_name = normalize_reference_name(raw_name)
    venue_value = str(venue or "") if kind == "product" else ""
    entry = (
        db.query(ReferenceCatalogEntry)
        .filter(
            ReferenceCatalogEntry.kind == kind,
            ReferenceCatalogEntry.venue == venue_value,
            ReferenceCatalogEntry.normalized_name == normalized_name,
        )
        .first()
    )
    created = entry is None
    if entry is None:
        entry = ReferenceCatalogEntry(kind=kind, venue=venue_value, raw_name=raw_name, normalized_name=normalized_name)
        db.add(entry)
    entry.raw_name = raw_name
    entry.external_id = external_id
    entry.external_name = external_name
    entry.unit = unit
    entry.status = status
    entry.confidence = float(confidence or 0.0)
    entry.source = source
    entry.candidates_json = json.dumps(candidates or [], ensure_ascii=False)
    entry.updated_at = datetime.utcnow()
    db.flush()
    return entry, created


def _entry_payload(entry: ReferenceCatalogEntry) -> dict[str, Any]:
    try:
        candidates = json.loads(entry.candidates_json or "[]")
    except json.JSONDecodeError:
        candidates = []
    return {
        "id": entry.id,
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
        return max(0.75, min(len(left), len(right)) / max(len(left), len(right)))
    return SequenceMatcher(None, left, right).ratio()
