"""Safe, serializable schema for evidence-backed tree case extraction."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


SCHEMA_VERSION = "1.0"
FIELD_NAMES = (
    "case_number",
    "address",
    "decision",
    "tree_count",
    "meeting_date",
)

Confidence = Literal["high", "medium", "low"]


@dataclass(frozen=True, slots=True)
class EvidenceField:
    value: str | int | None
    page: int | None
    quote_snippet: str | None
    confidence: Confidence | None

    @classmethod
    def null(cls) -> EvidenceField:
        return cls(value=None, page=None, quote_snippet=None, confidence=None)

    def to_dict(self) -> dict[str, str | int | None]:
        return {
            "value": self.value,
            "page": self.page,
            "quote_snippet": self.quote_snippet,
            "confidence": self.confidence,
        }


@dataclass(frozen=True, slots=True)
class Failure:
    source_pdf: str
    field: str
    reason: str

    def to_dict(self) -> dict[str, str]:
        return {
            "source_pdf": self.source_pdf,
            "field": self.field,
            "reason": self.reason,
        }


@dataclass(frozen=True, slots=True)
class ExtractedCase:
    schema_version: str
    source_pdf: str
    source_sha256: str
    model: str
    review_status: Literal["pending"]
    fields: dict[str, EvidenceField]

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "source_pdf": self.source_pdf,
            "source_sha256": self.source_sha256,
            "model": self.model,
            "review_status": self.review_status,
            "fields": {name: self.fields[name].to_dict() for name in FIELD_NAMES},
        }
