"""Compose the public-data pipeline with deterministic offline fixtures."""

from __future__ import annotations

import json
from contextlib import contextmanager
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import httpx
import pandas as pd

from scripts.config import SourceConfig
from scripts.crawl_review_records import crawl_records
from scripts.detect_anomalies import detect_anomalies
from scripts.extraction_schema import FIELD_NAMES
from scripts.extract_cases import process_directory
from scripts.fetch_opendata import fetch_dataset
from scripts.gap_report import build_gap_report
from scripts.health_check import build_health_report
from scripts.normalize import normalize_all


INDEX_URL = "https://culture.gov.taipei/meetings"
DETAIL_URL = "https://culture.gov.taipei/detail"
PDF_URL = "https://culture.gov.taipei/record.pdf"
DATA_URL = "https://data.taipei/street.csv"
FULL_PAGE = "Case A-1 at Oak Street approved 2 trees on 2026-07-01."
API_KEY_SENTINEL = "OFFLINE-API-KEY-MUST-NOT-LEAK"
_REPORT_NOW = datetime(2099, 7, 31, 12, tzinfo=UTC)


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


class _RouteClient:
    def __init__(self, routes: dict[str, httpx.Response]) -> None:
        self.routes = routes

    def get(self, url: str, **_kwargs: object) -> httpx.Response:
        return self.routes[url]

    @contextmanager
    def stream(self, _method: str, url: str, **_kwargs: object):
        yield self.routes[url]


class _HealthResponse:
    def __init__(self, content_type: str) -> None:
        self.status_code = 200
        self.headers = {"content-type": content_type}


class _HealthClient:
    def get(self, url: str, **_kwargs: object) -> _HealthResponse:
        content_type = "application/json" if "api/v1/dataset" in url else "text/html"
        return _HealthResponse(content_type)


class _ModelMessages:
    def __init__(self, response_text: str) -> None:
        self.response_text = response_text
        self.calls = 0

    def create(self, **_kwargs: object) -> object:
        self.calls += 1
        return SimpleNamespace(
            content=[SimpleNamespace(type="text", text=self.response_text)]
        )


class _ModelClient:
    def __init__(self, response_text: str) -> None:
        self.messages = _ModelMessages(response_text)
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
                "value": "approved",
                "page": 1,
                "quote_snippet": "approved",
                "confidence": "high",
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


def run_offline_smoke(repository: Path) -> dict[str, object]:
    """Run one offline pass and return stable semantic artifacts and run statuses."""
    snapshot_day = datetime.now(UTC).astimezone(ZoneInfo("Asia/Taipei")).date()
    previous_day = snapshot_day - timedelta(days=1)
    raw_open = repository / "raw" / "open_data"
    source = SourceConfig("street_trees", DATA_URL, None, True)
    old_csv = b"TreeID,Region,TreeType\nT-001,North,Banyan\nT-002,East,Camphor\n"
    new_csv = b"TreeID,Region,TreeType\nT-001,North,Banyan\n"
    old = fetch_dataset(
        source,
        raw_open,
        previous_day,
        _RouteClient({DATA_URL: _response(old_csv, "text/csv")}),
    )
    new = fetch_dataset(
        source,
        raw_open,
        snapshot_day,
        _RouteClient({DATA_URL: _response(new_csv, "text/csv")}),
    )

    processed = repository / "processed"
    normalized = normalize_all(raw_open, processed)
    anomaly = detect_anomalies(processed, now=_REPORT_NOW)
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
        _RouteClient(
            {
                INDEX_URL: _response(index, "text/html"),
                DETAIL_URL: _response('<a href="/record.pdf">PDF</a>', "text/html"),
                PDF_URL: _response(_text_pdf(FULL_PAGE), "application/pdf"),
            }
        ),
    )

    model = _ModelClient(_model_payload())
    batch = process_directory(
        repository / "raw" / "review_meetings",
        repository / "extracted",
        model,
        "offline-model",
        runner=lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("OCR binary must not run")
        ),
    )

    health_sources = {
        "protected_trees": SourceConfig("protected_trees", None, None, False),
        "review_records": SourceConfig("review_records", INDEX_URL, None, False),
        "street_trees": SourceConfig("street_trees", None, "street-id", True),
    }
    health = build_health_report(
        health_sources,
        _HealthClient(),
        previous_report=None,
        clock=lambda: _REPORT_NOW,
        sleeper=lambda _delay: None,
    )
    _write_json(reports / "health.json", health)
    gaps = build_gap_report(health, repository, clock=lambda: _REPORT_NOW)
    _write_json(reports / "gaps.json", gaps)

    frames = [
        pd.read_parquet(item.path).fillna("").to_dict(orient="records")
        for item in normalized
    ]
    cases = sorted(
        (
            json.loads(path.read_text(encoding="utf-8"))
            for path in (repository / "extracted").rglob("*.json")
            if path.name != "extraction_failures.json"
        ),
        key=lambda item: str(item["source_pdf"]),
    )
    failures = json.loads(
        (repository / "extracted" / "extraction_failures.json").read_text(
            encoding="utf-8"
        )
    )
    return {
        "fetch_statuses": [old.status, new.status],
        "review_statuses": [record.status for record in records],
        "normalized": frames,
        "anomaly": anomaly.to_dict(),
        "events": (processed / "tree_events.jsonl").read_text(encoding="utf-8"),
        "extractions": cases,
        "failures": failures["failures"],
        "health": health,
        "gaps": gaps,
        "batch": [batch.extracted_files, batch.failed_fields],
        "model_calls": model.messages.calls,
    }
