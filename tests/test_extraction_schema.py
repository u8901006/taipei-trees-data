from __future__ import annotations

from scripts.extraction_schema import (
    FIELD_NAMES,
    EvidenceField,
    ExtractedCase,
    Failure,
)


def test_evidence_field_serializes_exact_contract() -> None:
    field = EvidenceField(
        value="北投路一段",
        page=2,
        quote_snippet="地址：北投路一段",
        confidence="high",
    )

    assert field.to_dict() == {
        "value": "北投路一段",
        "page": 2,
        "quote_snippet": "地址：北投路一段",
        "confidence": "high",
    }


def test_extracted_case_serializes_exact_schema_and_pending_status() -> None:
    fields = {name: EvidenceField.null() for name in FIELD_NAMES}
    case = ExtractedCase(
        schema_version="1.0",
        source_pdf="2026-07/record.pdf",
        source_sha256="a" * 64,
        model="test-model",
        review_status="pending",
        fields=fields,
    )

    serialized = case.to_dict()

    assert set(serialized) == {
        "schema_version",
        "source_pdf",
        "source_sha256",
        "model",
        "review_status",
        "fields",
    }
    assert tuple(serialized["fields"]) == FIELD_NAMES
    assert serialized["review_status"] == "pending"
    assert all(value == EvidenceField.null().to_dict() for value in serialized["fields"].values())


def test_failure_serializes_only_safe_locator_and_reason() -> None:
    failure = Failure("2026-07/record.pdf", "address", "quote_not_exact")

    assert failure.to_dict() == {
        "source_pdf": "2026-07/record.pdf",
        "field": "address",
        "reason": "quote_not_exact",
    }
