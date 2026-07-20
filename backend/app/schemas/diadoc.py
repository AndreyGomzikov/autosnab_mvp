from typing import Any

from pydantic import BaseModel, Field


class DiadocOrganization(BaseModel):
    organization_id: str | None = None
    name: str | None = None
    inn: str | None = None
    kpp: str | None = None
    box_ids: list[str] = Field(default_factory=list)


class DiadocStatusResponse(BaseModel):
    enabled: bool
    configured: bool
    box_id: str | None = None
    last_sync_at: str | None = None
    after_index_key_present: bool = False
    last_error: str | None = None
    scheduler_enabled: bool = False
    sync_interval_seconds: int = 0
    initial_sync_mode: str = "latest"
    max_pages_per_sync: int = 0
    retry_queue_size: int = 0
    delivery_queue_size: int = 0
    dead_letter_count: int = 0
    delivery_dead_letter_count: int = 0
    oauth: dict[str, Any] = Field(default_factory=dict)


class DiadocSyncResponse(BaseModel):
    status: str
    pages_received: int = 0
    events_received: int = 0
    documents_discovered: int = 0
    documents_processed: int = 0
    documents_downloaded: int = 0
    documents_skipped: int = 0
    documents_failed: int = 0
    documents_retried: int = 0
    documents_recovered: int = 0
    deliveries_retried: int = 0
    deliveries_recovered: int = 0
    deliveries_failed: int = 0
    artifacts_downloaded: int = 0
    review_ids: list[int] = Field(default_factory=list)
    next_index_key_present: bool = False
