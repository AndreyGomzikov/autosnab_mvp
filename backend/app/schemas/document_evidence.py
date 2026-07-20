from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class EvidenceProviderAttempt(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider: str
    page_number: int | None = Field(default=None, ge=1)
    status: Literal["success", "error", "skipped"]
    duration_ms: int | None = None
    attempts: int = 1
    retryable: bool = False
    raw_text_length: int = 0
    pages: int | None = None
    error_type: str | None = None
    error_message: str | None = None


class EvidencePageSource(BaseModel):
    model_config = ConfigDict(extra="forbid")

    page_number: int = Field(ge=1)
    filename: str
    source_type: Literal["pdf", "image", "excel", "xml", "unknown"]
    original_path: str
    prepared_path: str | None = None
    transformations: list[str] = Field(default_factory=list)
    quality: dict[str, Any] = Field(default_factory=dict)


class DocumentEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid")

    evidence_version: str = "1.0"
    logical_document_id: str
    filename: str
    source_type: Literal["pdf", "image", "excel", "xml", "unknown"]
    ocr_used: bool = False
    extraction_method: str = ""
    raw_text: str = ""
    structured_document: Any | None = None
    pages: int | None = None
    page_sources: list[EvidencePageSource] = Field(default_factory=list)
    provider_attempts: list[EvidenceProviderAttempt] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)
    consistency_warnings: list[str] = Field(default_factory=list)
    error: str | None = None
