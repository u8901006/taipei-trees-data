"""Fully offline end-to-end smoke contract for the public-data pipeline."""

from __future__ import annotations

import gzip
import hashlib
import json
from datetime import datetime
from pathlib import Path

from scripts.offline_smoke import (
    API_KEY_SENTINEL,
    FULL_PAGE,
    _model_payload,
    run_offline_smoke,
)


def _raw_bytes(repository: Path) -> dict[str, bytes]:
    return {
        path.relative_to(repository).as_posix(): path.read_bytes()
        for path in sorted((repository / "raw").rglob("*"))
        if path.is_file()
    }


def _aware(value: str) -> bool:
    parsed = datetime.fromisoformat(value)
    return parsed.tzinfo is not None and parsed.utcoffset() is not None


def test_offline_pipeline_smoke_is_immutable_and_deterministic(tmp_path: Path) -> None:
    first = run_offline_smoke(tmp_path)
    immutable_before = _raw_bytes(tmp_path)
    second = run_offline_smoke(tmp_path)
    immutable_after = _raw_bytes(tmp_path)

    assert first["fetch_statuses"] == ["created", "created"]
    assert second["fetch_statuses"] == ["unchanged", "unchanged"]
    assert first["review_statuses"] == ["created"]
    assert second["review_statuses"] == ["unchanged"]
    assert first["batch"] == [1, 0]
    assert second["batch"] == [0, 0]
    assert first["model_calls"] == 1
    assert second["model_calls"] == 0
    assert immutable_before == immutable_after
    assert len(list((tmp_path / "raw" / "open_data").rglob("*.csv.gz"))) == 2
    assert len(list((tmp_path / "raw" / "review_meetings").rglob("*.pdf"))) == 1

    stable_keys = (
        "normalized",
        "anomaly",
        "events",
        "extractions",
        "failures",
        "health",
        "gaps",
    )
    assert {key: first[key] for key in stable_keys} == {
        key: second[key] for key in stable_keys
    }
    assert [len(frame) for frame in first["normalized"]] == [2, 1]
    assert first["anomaly"]["schema_version"] == "1.0"
    assert first["anomaly"]["found"] is True
    event = json.loads(str(first["events"]).strip())
    assert event["confidence"] == "inferred"
    assert event["tree_id"] == "T-002"

    raw_snapshot = max(
        (tmp_path / "raw" / "open_data" / "street_trees").glob("*.csv.gz")
    )
    raw_manifest = json.loads(
        raw_snapshot.with_suffix("").with_suffix(".json").read_text(encoding="utf-8")
    )
    raw_payload = gzip.decompress(raw_snapshot.read_bytes())
    assert raw_manifest["sha256"] == hashlib.sha256(raw_payload).hexdigest()
    assert raw_manifest["uncompressed_byte_length"] == len(raw_payload)
    assert _aware(raw_manifest["retrieved_at"])

    review_pdf = next((tmp_path / "raw" / "review_meetings").rglob("*.pdf"))
    review_manifest = json.loads(
        review_pdf.with_suffix(".manifest.json").read_text(encoding="utf-8")
    )
    assert review_manifest["schema_version"] == 1
    assert review_manifest["sha256"] == hashlib.sha256(review_pdf.read_bytes()).hexdigest()
    assert review_manifest["byte_length"] == review_pdf.stat().st_size
    assert _aware(review_manifest["retrieved_at"])

    health = first["health"]
    assert health["schema_version"] == "1.0"
    assert _aware(health["generated_at"])
    assert [source["name"] for source in health["sources"]] == [
        "protected_trees",
        "review_records",
        "street_trees",
    ]
    assert health["sources"][0]["status"] == "not_configured"
    gaps = first["gaps"]
    assert gaps["schema_version"] == "1.0"
    assert _aware(gaps["generated_at"])
    for source in gaps["sources"]:
        for path in source["evidence_paths"]:
            assert not Path(path).is_absolute()
            assert ".." not in Path(path).parts

    extraction_case = first["extractions"][0]
    assert extraction_case["schema_version"] == "1.0"
    assert extraction_case["review_status"] == "pending"
    assert extraction_case["fields"]["case_number"]["quote_snippet"] == "Case A-1"

    output_text = "\n".join(
        path.read_text(encoding="utf-8")
        for path in tmp_path.rglob("*")
        if path.is_file() and path.suffix in {".json", ".jsonl"}
    )
    assert API_KEY_SENTINEL not in output_text
    assert str(tmp_path.resolve()) not in output_text
    assert _model_payload() not in output_text
    assert FULL_PAGE not in output_text
