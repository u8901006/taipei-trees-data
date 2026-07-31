"""Fully offline end-to-end smoke contract for the public-data pipeline."""

from __future__ import annotations

import gzip
import hashlib
import json
import socket
from contextlib import contextmanager
from datetime import UTC, date, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import NoReturn

import httpx
import pandas as pd
import pytest

import scripts.extract_cases as extraction
import scripts.load_postgis as postgis
from scripts.config import SourceConfig
from scripts.crawl_review_records import crawl_records
from scripts.detect_anomalies import detect_anomalies
from scripts.extraction_schema import FIELD_NAMES
from scripts.fetch_opendata import fetch_dataset
from scripts.gap_report import build_gap_report
from scripts.health_check import build_health_report
from scripts.normalize import normalize_all


NOW = datetime(2026, 7, 31, 12, tzinfo=UTC)
INDEX_URL = "https://culture.gov.taipei/meetings"
DETAIL_URL = "https://culture.gov.taipei/detail"
PDF_URL = "https://culture.gov.taipei/record.pdf"
DATA_URL = "https://data.taipei/street.csv"
RAW_MODEL_SENTINEL = "RAW-MODEL-RESPONSE-MUST-NOT-LEAK"
FULL_PAGE_SENTINEL = "FULL-PAGE-TEXT-MUST-NOT-LEAK"
API_KEY_SENTINEL = "OFFLINE-API-KEY-MUST-NOT-LEAK"
PAGE_TEXT = (
    "Case A-1 at Oak Street concerns 2 trees on 2026-07-01. "
    f"{FULL_PAGE_SENTINEL}"
)


def _forbidden(*_args: object, **_kwargs: object) -> NoReturn:
    raise AssertionError("offline smoke attempted an external service")


def _install_external_service_guards(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(socket, "create_connection", _forbidden)
    monkeypatch.setattr(socket.socket, "connect", _forbidden)
    monkeypatch.setattr(httpx, "Client", _forbidden)
    monkeypatch.setattr(extraction, "_default_client_factory", _forbidden)
    monkeypatch.setattr(extraction.subprocess, "run", _forbidden)
    monkeypatch.setattr(postgis, "load_trees", _forbidden)


def _text_pdf(text: str) -> bytes:
    content = f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode("ascii")
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        (
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            b"/Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>"
        ),
        b"<< /Length " + str(len(content)).encode() + b" >>\nstream\n"
        + content
        + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    output = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for number, body in enumerate(objects, start=1):
        offsets.append(len(output))
        output.extend(f"{number} 0 obj\n".encode())
        output.extend(body)
        output.extend(b"\nendobj\n")
    xref = len(output)
    output.extend(f"xref\n0 {len(objects) + 1}\n".encode())
    output.extend(b"0000000000 65535 f \n")
    for offset in offsets[1:]:
        output.extend(f"{offset:010d} 00000 n \n".encode())
    output.extend(
        (
            f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\n"
            f"startxref\n{xref}\n%%EOF\n"
        ).encode()
    )
    return bytes(output)


def _response(content: bytes | str, content_type: str) -> httpx.Response:
    payload = content.encode("utf-8") if isinstance(content, str) else content
    return httpx.Response(
        200,
        content=payload,
        headers={"content-type": content_type, "content-length": str(len(payload))},
    )


class RouteClient:
    def __init__(self, routes: dict[str, httpx.Response]) -> None:
        self.routes = routes

    def get(self, url: str, **_kwargs: object) -> httpx.Response:
        return self.routes[url]

    @contextmanager
    def stream(self, _method: str, url: str, **_kwargs: object):
        yield self.routes[url]


class HealthResponse:
    def __init__(self, content_type: str) -> None:
        self.status_code = 200
        self.headers = {"content-type": content_type}


class HealthClient:
    def get(self, url: str, **_kwargs: object) -> HealthResponse:
        content_type = "application/json" if "api/v1/dataset" in url else "text/html"
        return HealthResponse(content_type)


class ModelMessages:
    def __init__(self, response_text: str) -> None:
        self.response_text = response_text
        self.calls = 0

    def create(self, **_kwargs: object) -> object:
        self.calls += 1
        return SimpleNamespace(
            content=[SimpleNamespace(type="text", text=self.response_text)]
        )


class ModelClient:
    def __init__(self, response_text: str) -> None:
        self.messages = ModelMessages(response_text)
        self.api_key = API_KEY_SENTINEL


def _model_payload() -> str:
    null = {
        "value": None,
        "page": None,
        "quote_snippet": None,
        "confidence": None,
    }
    fields = {name: dict(null) for name in FIELD_NAMES}
    fields.update(
        {
            "case_number": {
                "value": "A-1",
                "page": 1,
                "quote_snippet": "Case A-1",
                "confidence": "high",
            },
            "address": {
                "value": "Oak Street",
                "page": 1,
                "quote_snippet": "Oak Street",
                "confidence": "high",
            },
            "decision": {
                "value": RAW_MODEL_SENTINEL,
                "page": 1,
                "quote_snippet": RAW_MODEL_SENTINEL,
                "confidence": "low",
            },
            "tree_count": {
                "value": 2,
                "page": 1,
                "quote_snippet": "2 trees",
                "confidence": "high",
            },
            "meeting_date": {
                "value": "2026-07-01",
                "page": 1,
                "quote_snippet": "2026-07-01",
                "confidence": "high",
            },
        }
    )
    return json.dumps(fields, ensure_ascii=False)


def _write_json(path: Path, document: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(document, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )


def _run_pipeline(repository: Path, model: ModelClient) -> dict[str, object]:
    raw_open = repository / "raw" / "open_data"
    source = SourceConfig("street_trees", DATA_URL, None, True)
    old_csv = b"TreeID,Region,TreeType\nT-001,North,Banyan\nT-002,East,Camphor\n"
    new_csv = b"TreeID,Region,TreeType\nT-001,North,Banyan\n"
    old = fetch_dataset(
        source,
        raw_open,
        date(2026, 7, 30),
        RouteClient({DATA_URL: _response(old_csv, "text/csv")}),
        clock=lambda: datetime(2026, 7, 30, 12, tzinfo=UTC),
    )
    new = fetch_dataset(
        source,
        raw_open,
        date(2026, 7, 31),
        RouteClient({DATA_URL: _response(new_csv, "text/csv")}),
        clock=lambda: NOW,
    )

    processed = repository / "processed"
    normalized = normalize_all(raw_open, processed)
    anomaly = detect_anomalies(processed, now=NOW)
    reports = repository / "reports"
    _write_json(reports / "anomalies.json", anomaly.to_dict())

    title = (
        "〔會議紀錄〕115.07.01臺北市樹木保護委員會"
        "第15屆第22次幹事會會議紀錄"
    )
    index = f'<table><tr><td>115.07.01</td><td><a href="/detail">{title}</a></td></tr></table>'
    records = crawl_records(
        INDEX_URL,
        repository / "raw" / "review_meetings",
        "review",
        RouteClient(
            {
                INDEX_URL: _response(index, "text/html"),
                DETAIL_URL: _response('<a href="/record.pdf">PDF</a>', "text/html"),
                PDF_URL: _response(_text_pdf(PAGE_TEXT), "application/pdf"),
            }
        ),
        clock=lambda: NOW,
    )

    batch = extraction.process_directory(
        repository / "raw" / "review_meetings",
        repository / "extracted",
        model,
        "offline-model",
        runner=_forbidden,
        clock=lambda: NOW,
    )

    health_sources = {
        "committee_records": SourceConfig("committee_records", None, None, False),
        "protected_trees": SourceConfig("protected_trees", None, None, False),
        "pruning_schedule": SourceConfig("pruning_schedule", None, None, False),
        "review_records": SourceConfig("review_records", INDEX_URL, None, False),
        "street_trees": SourceConfig("street_trees", None, "street-id", True),
    }
    health = build_health_report(
        health_sources,
        HealthClient(),
        previous_report=None,
        clock=lambda: NOW,
        sleeper=lambda _delay: None,
    )
    _write_json(reports / "health.json", health)
    gaps = build_gap_report(health, repository, clock=lambda: NOW)
    _write_json(reports / "gaps.json", gaps)

    return {
        "fetch_statuses": [old.status, new.status],
        "review_statuses": [record.status for record in records],
        "normalized": [
            pd.read_parquet(item.path).fillna("").to_dict(orient="records")
            for item in normalized
        ],
        "anomaly": anomaly.to_dict(),
        "events": (processed / "tree_events.jsonl").read_text(encoding="utf-8"),
        "health": health,
        "gaps": gaps,
        "batch": [batch.extracted_files, batch.failed_fields],
    }


def _raw_bytes(repository: Path) -> dict[str, bytes]:
    return {
        path.relative_to(repository).as_posix(): path.read_bytes()
        for path in sorted((repository / "raw").rglob("*"))
        if path.is_file()
    }


def _aware(value: str) -> bool:
    parsed = datetime.fromisoformat(value)
    return parsed.tzinfo is not None and parsed.utcoffset() is not None


def _assert_relative_paths(paths: object) -> None:
    assert isinstance(paths, list)
    for value in paths:
        assert isinstance(value, str)
        assert not Path(value).is_absolute()
        assert ".." not in Path(value).parts


def test_offline_pipeline_smoke_is_immutable_and_deterministic(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_external_service_guards(monkeypatch)
    model = ModelClient(_model_payload())

    first = _run_pipeline(tmp_path, model)
    immutable_before = _raw_bytes(tmp_path)
    stable_text_before = {
        path.relative_to(tmp_path).as_posix(): path.read_bytes()
        for path in tmp_path.rglob("*")
        if path.is_file() and path.suffix in {".json", ".jsonl"}
    }
    second = _run_pipeline(tmp_path, model)

    assert first["fetch_statuses"] == ["created", "created"]
    assert second["fetch_statuses"] == ["unchanged", "unchanged"]
    assert first["review_statuses"] == ["created"]
    assert second["review_statuses"] == ["unchanged"]
    assert first["batch"] == [1, 1]
    assert second["batch"] == [0, 0]
    assert model.messages.calls == 1
    assert immutable_before == _raw_bytes(tmp_path)
    assert stable_text_before == {
        path.relative_to(tmp_path).as_posix(): path.read_bytes()
        for path in tmp_path.rglob("*")
        if path.is_file() and path.suffix in {".json", ".jsonl"}
    }

    assert (tmp_path / "processed" / "trees.parquet").is_file()
    assert len(pd.read_parquet(tmp_path / "processed" / "trees.parquet")) == 1
    snapshot_paths = sorted((tmp_path / "processed" / "snapshots").rglob("*.parquet"))
    assert len(snapshot_paths) == 2
    assert [len(frame) for frame in first["normalized"]] == [2, 1]
    for snapshot in snapshot_paths:
        schema = json.loads(snapshot.with_suffix(".schema.json").read_text(encoding="utf-8"))
        assert set(schema) == {
            "canonical_headers",
            "encoding",
            "original_headers",
            "row_count",
            "sha256",
        }
        raw = (
            tmp_path
            / "raw"
            / "open_data"
            / snapshot.parent.name
            / f"{snapshot.stem}.csv.gz"
        )
        payload = gzip.decompress(raw.read_bytes())
        assert schema["sha256"] == hashlib.sha256(payload).hexdigest()
        assert schema["row_count"] == len(pd.read_parquet(snapshot))

    raw_snapshots = sorted((tmp_path / "raw" / "open_data").rglob("*.csv.gz"))
    assert len(raw_snapshots) == 2
    for snapshot in raw_snapshots:
        manifest = json.loads(
            snapshot.with_suffix("").with_suffix(".json").read_text(encoding="utf-8")
        )
        payload = gzip.decompress(snapshot.read_bytes())
        assert manifest["sha256"] == hashlib.sha256(payload).hexdigest()
        assert manifest["uncompressed_byte_length"] == len(payload)
        assert _aware(manifest["retrieved_at"])
    assert [
        json.loads(
            snapshot.with_suffix("").with_suffix(".json").read_text(encoding="utf-8")
        )["retrieved_at"]
        for snapshot in raw_snapshots
    ] == [
        "2026-07-30T12:00:00+00:00",
        NOW.isoformat(),
    ]

    anomaly = first["anomaly"]
    assert anomaly["schema_version"] == "1.0"
    assert _aware(anomaly["generated_at"])
    assert anomaly["found"] is True
    event = json.loads(str(first["events"]).strip())
    assert event["confidence"] == "inferred"
    assert event["tree_id"] == "T-002"

    review_pdf = next((tmp_path / "raw" / "review_meetings").rglob("*.pdf"))
    review_manifest = json.loads(
        review_pdf.with_suffix(".manifest.json").read_text(encoding="utf-8")
    )
    assert review_manifest["schema_version"] == 1
    assert review_manifest["sha256"] == hashlib.sha256(review_pdf.read_bytes()).hexdigest()
    assert review_manifest["byte_length"] == review_pdf.stat().st_size
    assert review_manifest["retrieved_at"] == NOW.isoformat()

    case_path = next(
        path
        for path in (tmp_path / "extracted").rglob("*.json")
        if path.name != "extraction_failures.json"
    )
    case = json.loads(case_path.read_text(encoding="utf-8"))
    expected_source = review_pdf.relative_to(
        tmp_path / "raw" / "review_meetings"
    ).as_posix()
    assert set(case) == {
        "schema_version",
        "source_pdf",
        "source_sha256",
        "model",
        "review_status",
        "fields",
    }
    assert case["schema_version"] == "1.0"
    assert case["source_pdf"] == expected_source
    assert case["source_sha256"] == hashlib.sha256(review_pdf.read_bytes()).hexdigest()
    assert case["review_status"] == "pending"
    assert set(case["fields"]) == set(FIELD_NAMES)
    for field in case["fields"].values():
        assert set(field) == {"value", "page", "quote_snippet", "confidence"}
        if field["value"] is None:
            assert field == {
                "value": None,
                "page": None,
                "quote_snippet": None,
                "confidence": None,
            }
        else:
            assert field["page"] == 1
            assert field["quote_snippet"] in PAGE_TEXT
            assert field["confidence"] in {"high", "medium", "low"}
    assert case["fields"]["decision"]["value"] is None

    failure_wrapper = json.loads(
        (tmp_path / "extracted" / "extraction_failures.json").read_text(
            encoding="utf-8"
        )
    )
    assert set(failure_wrapper) == {"schema_version", "generated_at", "failures"}
    assert failure_wrapper["schema_version"] == "1.0"
    assert failure_wrapper["generated_at"] == NOW.isoformat()
    assert failure_wrapper["failures"] == [
        {
            "field": "decision",
            "reason": "quote_not_exact",
            "source_pdf": expected_source,
        }
    ]

    health = first["health"]
    assert health["schema_version"] == "1.0"
    assert _aware(health["generated_at"])
    assert [
        (source["name"], source["kind"], source["status"], source["required"])
        for source in health["sources"]
    ] == [
        ("committee_records", "url", "not_configured", False),
        ("protected_trees", "dataset", "not_configured", False),
        ("pruning_schedule", "url", "not_configured", False),
        ("review_records", "url", "available", False),
        ("street_trees", "dataset", "available", True),
    ]

    gaps = first["gaps"]
    assert gaps["schema_version"] == "1.0"
    assert _aware(gaps["generated_at"])
    for source in gaps["sources"]:
        _assert_relative_paths(source["evidence_paths"])
    for gap in gaps["gaps"]:
        _assert_relative_paths(gap["evidence_paths"])
    gap_sources = {source["name"]: source for source in gaps["sources"]}
    assert len(gap_sources["street_trees"]["evidence_paths"]) == 2
    assert len(gap_sources["review_records"]["evidence_paths"]) == 2
    pending_gap = next(
        gap
        for gap in gaps["gaps"]
        if gap["code"] == "pending_extraction_review"
    )
    assert pending_gap["evidence_paths"] == [
        case_path.relative_to(tmp_path).as_posix()
    ]
    failure_gap = next(
        gap for gap in gaps["gaps"] if gap["code"] == "extraction_failures"
    )
    assert failure_gap["evidence_paths"] == [
        "extracted/extraction_failures.json"
    ]

    output_text = "\n".join(
        path.read_text(encoding="utf-8")
        for path in tmp_path.rglob("*")
        if path.is_file()
        and (
            path.suffix in {".json", ".jsonl", ".csv", ".txt", ".md"}
            or path.name.endswith(".schema.json")
        )
    )
    assert API_KEY_SENTINEL not in output_text
    assert RAW_MODEL_SENTINEL not in output_text
    assert FULL_PAGE_SENTINEL not in output_text
    assert str(tmp_path.resolve()) not in output_text
