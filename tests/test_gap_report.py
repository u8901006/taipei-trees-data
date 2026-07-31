from __future__ import annotations

import gzip
import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from scripts.gap_report import GapInputError, STALE_AFTER_DAYS, build_gap_report, main


NOW = datetime(2026, 7, 31, 12, 0, tzinfo=UTC)
REVIEW_TITLE = "〔會議紀錄〕115.07.08臺北市樹木保護委員會第15屆第22次幹事會會議紀錄"
COMMITTEE_TITLE = "〔會議紀錄〕115.03.18臺北市樹木保護委員會第15屆第2次委員會會議紀錄"


def health_source(
    name: str,
    status: str,
    *,
    required: bool = False,
    unavailable_since: str | None = None,
) -> dict[str, object]:
    reason = {
        "available": None,
        "unavailable": "probe_failed",
        "not_configured": "source_not_configured",
    }[status]
    return {
        "name": name,
        "kind": "dataset" if name in {"street_trees", "protected_trees"} else "url",
        "required": required,
        "status": status,
        "checked_at": NOW.isoformat(),
        "reason": reason,
        "unavailable_since": unavailable_since,
    }


def health_document(sources: list[dict[str, object]]) -> dict[str, object]:
    return {
        "schema_version": "1.0",
        "generated_at": NOW.isoformat(),
        "sources": sources,
    }


def write_open_snapshot(base: Path, source: str, day: str) -> tuple[Path, Path]:
    snapshot = base / "raw" / "open_data" / source / f"{day}.csv.gz"
    snapshot.parent.mkdir(parents=True, exist_ok=True)
    raw = b"TreeID\nT-1\n"
    snapshot.write_bytes(gzip.compress(raw, mtime=0))
    manifest = snapshot.with_suffix("").with_suffix(".json")
    manifest.write_text(
        json.dumps(
            {
                "source_name": source,
                "dataset_id": "dataset-id",
                "resource_id": "resource-id",
                "original_url": "https://data.taipei/download.csv",
                "retrieved_at": f"{day}T01:00:00+00:00",
                "uncompressed_byte_length": len(raw),
                "sha256": hashlib.sha256(raw).hexdigest(),
            }
        ),
        encoding="utf-8",
    )
    return snapshot, manifest


def write_schedule_snapshot(base: Path, day: str) -> tuple[Path, Path]:
    snapshot = base / "raw" / "pruning_schedules" / day / "schedule.csv"
    snapshot.parent.mkdir(parents=True, exist_ok=True)
    snapshot.write_text("date,address\n", encoding="utf-8")
    manifest = snapshot.with_suffix(".manifest.json")
    manifest.write_text(
        json.dumps(
            {
                "schema_version": "1.0",
                "source_url": "https://parks.gov.taipei/schedule.csv",
                "sha256": hashlib.sha256(snapshot.read_bytes()).hexdigest(),
                "byte_length": snapshot.stat().st_size,
                "content_type": "text/csv",
                "retrieved_at": f"{day}T02:00:00+00:00",
            }
        ),
        encoding="utf-8",
    )
    return snapshot, manifest


def write_review_pdf(
    base: Path,
    day: str,
    *,
    title: str = REVIEW_TITLE,
    filename: str = "record.pdf",
) -> tuple[Path, Path]:
    pdf = base / "raw" / "review_meetings" / day[:7] / filename
    pdf.parent.mkdir(parents=True, exist_ok=True)
    pdf.write_bytes(b"%PDF-1.7 fixture")
    manifest = pdf.with_suffix(".manifest.json")
    manifest.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "title": title,
                "published_date": day,
                "detail_url": "https://culture.gov.taipei/detail",
                "attachment_url": "https://culture.gov.taipei/record.pdf",
                "sha256": hashlib.sha256(pdf.read_bytes()).hexdigest(),
                "byte_length": pdf.stat().st_size,
                "retrieved_at": f"{day}T03:00:00+00:00",
            }
        ),
        encoding="utf-8",
    )
    return pdf, manifest


def text_pdf_bytes(text: str = "Case A-1 address Oak Street approved 2 trees 2026-07-30") -> bytes:
    content = f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode("ascii")
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        (
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            b"/Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>"
        ),
        b"<< /Length " + str(len(content)).encode("ascii") + b" >>\nstream\n"
        + content
        + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    output = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for number, body in enumerate(objects, start=1):
        offsets.append(len(output))
        output.extend(f"{number} 0 obj\n".encode("ascii"))
        output.extend(body)
        output.extend(b"\nendobj\n")
    xref = len(output)
    output.extend(f"xref\n0 {len(objects) + 1}\n".encode("ascii"))
    output.extend(b"0000000000 65535 f \n")
    for offset in offsets[1:]:
        output.extend(f"{offset:010d} 00000 n \n".encode("ascii"))
    output.extend(
        (
            f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\n"
            f"startxref\n{xref}\n%%EOF\n"
        ).encode("ascii")
    )
    return bytes(output)


def null_evidence_fields() -> dict[str, dict[str, object]]:
    return {
        name: {
            "value": None,
            "page": None,
            "quote_snippet": None,
            "confidence": None,
        }
        for name in ("case_number", "address", "decision", "tree_count", "meeting_date")
    }


def write_pending_case(
    base: Path,
    output_name: str,
    source_pdf: str,
) -> tuple[Path, Path]:
    pdf = base / "raw" / "review_meetings" / Path(source_pdf)
    pdf.parent.mkdir(parents=True, exist_ok=True)
    pdf.write_bytes(text_pdf_bytes())
    output = base / "extracted" / output_name
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(
            {
                "schema_version": "1.0",
                "source_pdf": source_pdf,
                "source_sha256": hashlib.sha256(pdf.read_bytes()).hexdigest(),
                "model": "SENTINEL-RAW-MODEL",
                "review_status": "pending",
                "fields": null_evidence_fields(),
            }
        ),
        encoding="utf-8",
    )
    return output, pdf


def gap_codes(report: dict[str, object]) -> list[str]:
    return [gap["code"] for gap in report["gaps"]]  # type: ignore[index]


def mutate_json(path: Path, **changes: object) -> None:
    document = json.loads(path.read_text(encoding="utf-8"))
    document.update(changes)
    path.write_text(json.dumps(document), encoding="utf-8")


@pytest.mark.parametrize(
    "mutation",
    ["corrupt_gzip", "wrong_length", "wrong_hash", "unsafe_url", "future", "date_mismatch"],
)
def test_open_data_manifest_must_match_safe_bytes_and_taipei_snapshot_date(
    tmp_path: Path,
    mutation: str,
) -> None:
    day = "2026-08-01" if mutation == "future" else "2026-07-30"
    snapshot, manifest = write_open_snapshot(tmp_path, "protected_trees", day)
    if mutation == "corrupt_gzip":
        snapshot.write_bytes(b"not gzip")
    elif mutation == "wrong_length":
        mutate_json(manifest, uncompressed_byte_length=999)
    elif mutation == "wrong_hash":
        mutate_json(manifest, sha256="0" * 64)
    elif mutation == "unsafe_url":
        mutate_json(manifest, original_url="https://evil.example/data?token=do-not-leak")
    elif mutation == "date_mismatch":
        mutate_json(manifest, retrieved_at="2026-07-29T01:00:00+00:00")
    health = health_document([health_source("protected_trees", "available")])

    report = build_gap_report(health, tmp_path, clock=lambda: NOW)

    assert "missing_protected_trees" in gap_codes(report)
    source = report["sources"][0]  # type: ignore[index]
    assert source["evidence_paths"] == []
    assert "do-not-leak" not in json.dumps(report, ensure_ascii=False)


@pytest.mark.parametrize(
    "mutation",
    ["wrong_length", "wrong_hash", "unsafe_url", "future", "future_time", "date_mismatch"],
)
def test_schedule_manifest_must_match_bytes_and_taipei_partition_date(
    tmp_path: Path,
    mutation: str,
) -> None:
    day = "2026-08-01" if mutation == "future" else "2026-07-30"
    snapshot, manifest = write_schedule_snapshot(tmp_path, day)
    if mutation == "wrong_length":
        mutate_json(manifest, byte_length=999)
    elif mutation == "wrong_hash":
        mutate_json(manifest, sha256="0" * 64)
    elif mutation == "unsafe_url":
        mutate_json(manifest, source_url="http://parks.gov.taipei/unsafe")
    elif mutation == "future_time":
        mutate_json(manifest, retrieved_at="2026-07-31T13:00:00+00:00")
        moved = snapshot.parent.parent / "2026-07-31"
        moved.mkdir()
        snapshot.replace(moved / snapshot.name)
        manifest.replace(moved / manifest.name)
    elif mutation == "date_mismatch":
        mutate_json(manifest, retrieved_at="2026-07-29T02:00:00+00:00")
    health = health_document([health_source("pruning_schedule", "available")])

    report = build_gap_report(health, tmp_path, clock=lambda: NOW)

    assert "missing_pruning_schedule" in gap_codes(report)
    assert report["sources"][0]["evidence_paths"] == []  # type: ignore[index]


@pytest.mark.parametrize(
    "mutation",
    ["bad_magic", "wrong_length", "wrong_hash", "unsafe_url", "future", "date_mismatch"],
)
def test_review_manifest_must_match_pdf_bytes_official_urls_and_taipei_date(
    tmp_path: Path,
    mutation: str,
) -> None:
    day = "2026-08-01" if mutation == "future" else "2026-07-28"
    pdf, manifest = write_review_pdf(tmp_path, day)
    if mutation == "bad_magic":
        pdf.write_bytes(b"not a PDF")
        mutate_json(
            manifest,
            byte_length=pdf.stat().st_size,
            sha256=hashlib.sha256(pdf.read_bytes()).hexdigest(),
        )
    elif mutation == "wrong_length":
        mutate_json(manifest, byte_length=999)
    elif mutation == "wrong_hash":
        mutate_json(manifest, sha256="0" * 64)
    elif mutation == "unsafe_url":
        mutate_json(manifest, attachment_url="https://evil.example/do-not-leak.pdf")
    elif mutation == "date_mismatch":
        mutate_json(manifest, retrieved_at="2026-07-27T03:00:00+00:00")
    health = health_document([health_source("review_records", "available")])

    report = build_gap_report(health, tmp_path, clock=lambda: NOW)

    assert report["sources"][0]["evidence_paths"] == []  # type: ignore[index]
    assert "do-not-leak" not in json.dumps(report, ensure_ascii=False)


def test_review_retrieval_may_follow_publication_but_not_precede_it(
    tmp_path: Path,
) -> None:
    pdf, manifest = write_review_pdf(tmp_path, "2026-07-01")
    mutate_json(manifest, retrieved_at="2026-07-30T03:00:00+00:00")
    health = health_document([health_source("review_records", "available")])

    report = build_gap_report(health, tmp_path, clock=lambda: NOW)

    assert report["sources"][0]["evidence_paths"] == sorted(  # type: ignore[index]
        path.relative_to(tmp_path).as_posix() for path in (pdf, manifest)
    )


def test_corrupt_deflate_with_gzip_header_is_a_fixed_safe_cli_error(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    snapshot, _manifest = write_open_snapshot(tmp_path, "protected_trees", "2026-07-30")
    snapshot.write_bytes(b"\x1f\x8b\x08\x00\x00\x00\x00\x00\x00\xff\xff\xff\xff")
    health = tmp_path / "health.json"
    health.write_text(
        json.dumps(health_document([health_source("protected_trees", "available")])),
        encoding="utf-8",
    )

    assert main(
        [
            "--health",
            str(health),
            "--out",
            str(tmp_path / "gaps.json"),
            "--base-dir",
            str(tmp_path),
        ],
        clock=lambda: NOW,
    ) == 0
    captured = capsys.readouterr()
    assert captured.err == ""
    report = json.loads((tmp_path / "gaps.json").read_text(encoding="utf-8"))
    assert "missing_protected_trees" in gap_codes(report)


def test_review_and_committee_sources_use_only_their_crawl_title_taxonomy(
    tmp_path: Path,
) -> None:
    committee = write_review_pdf(
        tmp_path,
        "2026-07-28",
        title=COMMITTEE_TITLE,
        filename="committee.pdf",
    )
    review = write_review_pdf(
        tmp_path,
        "2026-07-28",
        title=REVIEW_TITLE,
        filename="review.pdf",
    )
    health = health_document(
        [
            health_source("committee_records", "available"),
            health_source("review_records", "available"),
        ]
    )

    report = build_gap_report(health, tmp_path, clock=lambda: NOW)
    sources = {source["name"]: source for source in report["sources"]}  # type: ignore[index]

    assert sources["committee_records"]["evidence_paths"] == sorted(
        path.relative_to(tmp_path).as_posix() for path in committee
    )
    assert sources["review_records"]["evidence_paths"] == sorted(
        path.relative_to(tmp_path).as_posix() for path in review
    )


@pytest.mark.parametrize(
    "mutation",
    [
        lambda report: report.pop("generated_at"),
        lambda report: report.update({"extra": True}),
        lambda report: report["sources"][0].pop("reason"),
        lambda report: report["sources"][0].update({"status": "unknown"}),
        lambda report: report["sources"][0].update({"unavailable_since": NOW.isoformat()}),
        lambda report: report.update({"sources": list(reversed(report["sources"]))}),
    ],
)
def test_health_input_requires_exact_schema_status_contract_and_stable_order(
    tmp_path: Path,
    mutation: object,
) -> None:
    health = health_document(
        [
            health_source("a_source", "available"),
            health_source("z_source", "available"),
        ]
    )
    assert callable(mutation)
    mutation(health)

    with pytest.raises(GapInputError, match="gap input is invalid"):
        build_gap_report(health, tmp_path, clock=lambda: NOW)


def test_health_checked_at_cannot_be_later_than_report_generated_at(tmp_path: Path) -> None:
    health = health_document([health_source("street_trees", "available")])
    health["sources"][0]["checked_at"] = "2026-07-31T12:00:01+00:00"  # type: ignore[index]

    with pytest.raises(GapInputError, match="gap input is invalid"):
        build_gap_report(health, tmp_path, clock=lambda: NOW)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("generated_at", "2026-07-31T12:00:01+00:00"),
        ("reason", "unsafe_source_url"),
    ],
)
def test_health_requires_exact_producer_time_and_reason_contract(
    tmp_path: Path,
    field: str,
    value: str,
) -> None:
    health = health_document(
        [
            health_source(
                "street_trees",
                "unavailable",
                unavailable_since="2026-07-30T12:00:00+00:00",
            )
        ]
    )
    if field == "generated_at":
        health["generated_at"] = value
    else:
        health["sources"][0]["reason"] = value  # type: ignore[index]

    with pytest.raises(GapInputError, match="gap input is invalid"):
        build_gap_report(health, tmp_path, clock=lambda: NOW)


def test_sources_are_stable_and_messages_distinguish_continuity_from_no_configuration(
    tmp_path: Path,
) -> None:
    health = health_document(
        [
            health_source("protected_trees", "not_configured"),
            health_source(
                "review_records",
                "unavailable",
                unavailable_since="2026-06-15T08:00:00+00:00",
            ),
            health_source("street_trees", "available", required=True),
        ]
    )
    write_open_snapshot(tmp_path, "street_trees", "2026-07-30")

    report = build_gap_report(health, tmp_path, clock=lambda: NOW)
    sources = {source["name"]: source for source in report["sources"]}  # type: ignore[index]

    assert [source["name"] for source in report["sources"]] == [  # type: ignore[index]
        "protected_trees",
        "review_records",
        "street_trees",
    ]
    assert sources["review_records"]["message"] == "本資料自 2026-06-15 起未能更新"
    assert "尚未設定" in sources["protected_trees"]["message"]
    assert "不能視為目前已有涵蓋" in sources["protected_trees"]["message"]
    assert sources["street_trees"]["required"] is True
    assert sources["street_trees"]["snapshot_age_days"] == 1


def test_stale_threshold_is_strictly_more_than_30_whole_days(
    tmp_path: Path,
) -> None:
    assert STALE_AFTER_DAYS == 30
    health = health_document(
        [
            health_source("protected_trees", "available"),
            health_source("street_trees", "available"),
        ]
    )
    write_open_snapshot(tmp_path, "protected_trees", "2026-06-30")
    write_open_snapshot(tmp_path, "street_trees", "2026-07-01")

    report = build_gap_report(health, tmp_path, clock=lambda: NOW)
    stale = [
        gap for gap in report["gaps"] if gap["code"] == "stale_snapshot"  # type: ignore[index]
    ]

    assert [(gap["source"], gap["age_days"]) for gap in stale] == [
        ("protected_trees", 31)
    ]


def test_missing_protected_and_pruning_artifacts_are_explicit_even_when_health_available(
    tmp_path: Path,
) -> None:
    health = health_document(
        [
            health_source("protected_trees", "available"),
            health_source("pruning_schedule", "available"),
        ]
    )

    report = build_gap_report(health, tmp_path, clock=lambda: NOW)

    assert "missing_protected_trees" in gap_codes(report)
    assert "missing_pruning_schedule" in gap_codes(report)


def test_valid_protected_pruning_and_review_artifacts_use_relative_stable_paths(
    tmp_path: Path,
) -> None:
    health = health_document(
        [
            health_source("protected_trees", "available"),
            health_source("pruning_schedule", "available"),
            health_source("review_records", "available"),
        ]
    )
    protected = write_open_snapshot(tmp_path, "protected_trees", "2026-07-29")
    pruning = write_schedule_snapshot(tmp_path, "2026-07-30")
    review = write_review_pdf(tmp_path, "2026-07-28")

    report = build_gap_report(health, tmp_path, clock=lambda: NOW)
    sources = {source["name"]: source for source in report["sources"]}  # type: ignore[index]

    assert "missing_protected_trees" not in gap_codes(report)
    assert "missing_pruning_schedule" not in gap_codes(report)
    assert sources["protected_trees"]["evidence_paths"] == sorted(
        path.relative_to(tmp_path).as_posix() for path in protected
    )
    assert sources["pruning_schedule"]["evidence_paths"] == sorted(
        path.relative_to(tmp_path).as_posix() for path in pruning
    )
    assert sources["review_records"]["evidence_paths"] == sorted(
        path.relative_to(tmp_path).as_posix() for path in review
    )
    assert str(tmp_path.resolve()) not in json.dumps(report, ensure_ascii=False)


@pytest.mark.parametrize(
    "mutation",
    ["wrong_hash", "absolute_source", "missing_field", "bad_null_contract", "missing_pdf"],
)
def test_pending_extraction_requires_exact_task5b_case_and_bound_source_pdf(
    tmp_path: Path,
    mutation: str,
) -> None:
    output, pdf = write_pending_case(tmp_path, "case.json", "2026-07/case.pdf")
    document = json.loads(output.read_text(encoding="utf-8"))
    if mutation == "wrong_hash":
        document["source_sha256"] = "0" * 64
    elif mutation == "absolute_source":
        document["source_pdf"] = str(pdf.resolve())
    elif mutation == "missing_field":
        del document["fields"]["address"]
    elif mutation == "bad_null_contract":
        document["fields"]["address"]["page"] = 1
    elif mutation == "missing_pdf":
        pdf.unlink()
    output.write_text(json.dumps(document), encoding="utf-8")
    health = health_document([health_source("review_records", "available")])

    report = build_gap_report(health, tmp_path, clock=lambda: NOW)

    assert "pending_extraction_review" not in gap_codes(report)


@pytest.mark.parametrize(
    "mutation",
    ["page_999", "fabricated_quote", "overlong_quote", "full_page_quote"],
)
def test_pending_extraction_revalidates_exact_task5b_page_evidence(
    tmp_path: Path,
    mutation: str,
) -> None:
    output, _pdf = write_pending_case(tmp_path, "case.json", "2026-07/case.pdf")
    document = json.loads(output.read_text(encoding="utf-8"))
    evidence = {
        "value": "A-1",
        "page": 1,
        "quote_snippet": "Case A-1",
        "confidence": "high",
    }
    document["fields"]["case_number"] = evidence
    if mutation == "page_999":
        evidence["page"] = 999
    elif mutation == "fabricated_quote":
        evidence["quote_snippet"] = "Case FABRICATED"
    elif mutation == "overlong_quote":
        evidence["quote_snippet"] = "x" * 501
    elif mutation == "full_page_quote":
        evidence["quote_snippet"] = (
            "Case A-1 address Oak Street approved 2 trees 2026-07-30"
        )
    output.write_text(json.dumps(document), encoding="utf-8")

    report = build_gap_report(
        health_document([health_source("review_records", "available")]),
        tmp_path,
        clock=lambda: NOW,
    )

    assert "pending_extraction_review" not in gap_codes(report)


def test_pending_extraction_accepts_exact_quote_from_verified_pdf_page(
    tmp_path: Path,
) -> None:
    output, _pdf = write_pending_case(tmp_path, "case.json", "2026-07/case.pdf")
    document = json.loads(output.read_text(encoding="utf-8"))
    document["fields"]["case_number"] = {
        "value": "A-1",
        "page": 1,
        "quote_snippet": "Case A-1",
        "confidence": "high",
    }
    output.write_text(json.dumps(document), encoding="utf-8")

    report = build_gap_report(
        health_document([health_source("review_records", "available")]),
        tmp_path,
        clock=lambda: NOW,
    )

    pending = next(
        gap
        for gap in report["gaps"]  # type: ignore[index]
        if gap["code"] == "pending_extraction_review"
    )
    assert pending["count"] == 1


def test_blank_text_pdf_cannot_count_as_pending_extraction_evidence(
    tmp_path: Path,
) -> None:
    output, pdf = write_pending_case(tmp_path, "case.json", "2026-07/case.pdf")
    pdf.write_bytes(text_pdf_bytes(""))
    document = json.loads(output.read_text(encoding="utf-8"))
    document["source_sha256"] = hashlib.sha256(pdf.read_bytes()).hexdigest()
    output.write_text(json.dumps(document), encoding="utf-8")

    report = build_gap_report(
        health_document([health_source("review_records", "available")]),
        tmp_path,
        clock=lambda: NOW,
    )

    assert "pending_extraction_review" not in gap_codes(report)


@pytest.mark.parametrize(
    "mutation",
    [
        "extra_root",
        "bad_schema",
        "naive_timestamp",
        "extra_entry",
        "absolute_source",
        "bad_field",
        "bad_reason",
        "duplicate_key",
    ],
)
def test_malformed_extraction_failure_history_fails_closed(
    tmp_path: Path,
    mutation: str,
) -> None:
    extracted = tmp_path / "extracted"
    extracted.mkdir()
    failure = {
        "source_pdf": "2026-07/case.pdf",
        "field": "address",
        "reason": "quote_not_exact",
    }
    document: dict[str, object] = {
        "schema_version": "1.0",
        "generated_at": NOW.isoformat(),
        "failures": [failure],
    }
    if mutation == "extra_root":
        document["extra"] = True
    elif mutation == "bad_schema":
        document["schema_version"] = "2.0"
    elif mutation == "naive_timestamp":
        document["generated_at"] = "2026-07-31T12:00:00"
    elif mutation == "extra_entry":
        failure["extra"] = True
    elif mutation == "absolute_source":
        failure["source_pdf"] = str((tmp_path / "case.pdf").resolve())
    elif mutation == "bad_field":
        failure["field"] = "page_text"
    elif mutation == "bad_reason":
        failure["reason"] = "raw model said no"
    path = extracted / "extraction_failures.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    if mutation == "duplicate_key":
        path.write_text(
            '{"schema_version":"1.0","schema_version":"1.0",'
            f'"generated_at":"{NOW.isoformat()}","failures":[]}}',
            encoding="utf-8",
        )

    with pytest.raises(GapInputError, match="gap input is invalid"):
        build_gap_report(
            health_document([health_source("review_records", "available")]),
            tmp_path,
            clock=lambda: NOW,
        )


def test_pending_extractions_and_failure_history_report_counts_without_content_leakage(
    tmp_path: Path,
) -> None:
    health = health_document([health_source("review_records", "available")])
    extracted = tmp_path / "extracted"
    write_pending_case(tmp_path, "z.json", "2026-07/z.pdf")
    write_pending_case(tmp_path, "a.json", "2026-07/a.pdf")
    (extracted / "extraction_failures.json").write_text(
        json.dumps(
            {
                "schema_version": "1.0",
                "generated_at": NOW.isoformat(),
                "failures": [
                    {
                        "source_pdf": "2026-07/a.pdf",
                        "field": "address",
                        "reason": "quote_not_exact",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    report = build_gap_report(health, tmp_path, clock=lambda: NOW)
    pending = next(
        gap
        for gap in report["gaps"]  # type: ignore[index]
        if gap["code"] == "pending_extraction_review"
    )
    failures = next(
        gap
        for gap in report["gaps"]  # type: ignore[index]
        if gap["code"] == "extraction_failures"
    )
    serialized = json.dumps(report, ensure_ascii=False)

    assert pending["count"] == 2
    assert pending["evidence_paths"] == ["extracted/a.json", "extracted/z.json"]
    assert failures["count"] == 1
    assert failures["evidence_paths"] == ["extracted/extraction_failures.json"]
    assert "SENTINEL-RAW-MODEL" not in serialized
    assert "2026-07/a.pdf" not in serialized


def test_report_has_exact_versioned_shape_summary_and_stable_gap_order(
    tmp_path: Path,
) -> None:
    health = health_document(
        [
            health_source("protected_trees", "not_configured"),
            health_source("pruning_schedule", "not_configured"),
        ]
    )

    report = build_gap_report(health, tmp_path, clock=lambda: NOW)

    assert set(report) == {
        "schema_version",
        "generated_at",
        "stale_after_days",
        "summary",
        "sources",
        "gaps",
    }
    assert set(report["summary"]) == {  # type: ignore[arg-type]
        "source_count",
        "available_sources",
        "unavailable_sources",
        "not_configured_sources",
        "gap_count",
    }
    gaps = report["gaps"]  # type: ignore[assignment]
    assert [(gap["code"], gap["source"] or "") for gap in gaps] == sorted(
        (gap["code"], gap["source"] or "") for gap in gaps
    )
    assert report["summary"]["gap_count"] == len(gaps)  # type: ignore[index]


def test_cli_missing_or_malformed_health_fails_safely_without_writing(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    health_path = tmp_path / "health.json"
    out_path = tmp_path / "gaps.json"
    assert main(
        ["--health", str(health_path), "--out", str(out_path), "--base-dir", str(tmp_path)],
        clock=lambda: NOW,
    ) == 1
    health_path.write_text('{"secret":"do-not-echo"}', encoding="utf-8")
    assert main(
        ["--health", str(health_path), "--out", str(out_path), "--base-dir", str(tmp_path)],
        clock=lambda: NOW,
    ) == 1

    captured = capsys.readouterr()
    assert "do-not-echo" not in captured.err
    assert str(tmp_path.resolve()) not in captured.err
    assert not out_path.exists()


def test_duplicate_key_health_file_is_rejected_by_cli(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    health = tmp_path / "health.json"
    health.write_text(
        '{"schema_version":"1.0","schema_version":"1.0",'
        f'"generated_at":"{NOW.isoformat()}","sources":[]}}',
        encoding="utf-8",
    )

    assert main(
        [
            "--health",
            str(health),
            "--out",
            str(tmp_path / "gaps.json"),
            "--base-dir",
            str(tmp_path),
        ],
        clock=lambda: NOW,
    ) == 1
    assert str(tmp_path.resolve()) not in capsys.readouterr().err


def test_symlinked_snapshot_outside_base_is_not_reported(
    tmp_path: Path,
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    external = tmp_path_factory.mktemp("gap-external") / "2026-07-30.csv.gz"
    raw = b"TreeID\nT-1\n"
    external.write_bytes(gzip.compress(raw, mtime=0))
    snapshot = tmp_path / "raw" / "open_data" / "protected_trees" / external.name
    snapshot.parent.mkdir(parents=True)
    try:
        snapshot.symlink_to(external)
    except OSError:
        pytest.skip("symlink creation is unavailable")
    manifest = snapshot.with_suffix("").with_suffix(".json")
    manifest.write_text(
        json.dumps(
            {
                "source_name": "protected_trees",
                "dataset_id": "dataset-id",
                "resource_id": "resource-id",
                "original_url": "https://data.taipei/download.csv",
                "retrieved_at": "2026-07-30T01:00:00+00:00",
                "uncompressed_byte_length": len(raw),
                "sha256": hashlib.sha256(raw).hexdigest(),
            }
        ),
        encoding="utf-8",
    )

    report = build_gap_report(
        health_document([health_source("protected_trees", "available")]),
        tmp_path,
        clock=lambda: NOW,
    )

    assert "missing_protected_trees" in gap_codes(report)
    assert report["sources"][0]["evidence_paths"] == []  # type: ignore[index]


def test_traversal_oserror_becomes_fixed_gap_input_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "extracted").mkdir()

    def failed_rglob(_self: Path, _pattern: str):
        raise OSError("absolute-path-do-not-leak")

    monkeypatch.setattr(Path, "rglob", failed_rglob)

    with pytest.raises(GapInputError, match="gap input is invalid") as caught:
        build_gap_report(
            health_document([health_source("street_trees", "available")]),
            tmp_path,
            clock=lambda: NOW,
        )
    assert "absolute-path-do-not-leak" not in str(caught.value)


def test_cleanup_oserror_is_caught_by_cli_without_path_leak(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    health = tmp_path / "health.json"
    health.write_text(json.dumps(health_document([])), encoding="utf-8")

    def failed_unlink(_self: Path, *args: object, **kwargs: object) -> None:
        raise OSError("absolute-path-do-not-leak")

    monkeypatch.setattr(Path, "unlink", failed_unlink)

    assert main(
        [
            "--health",
            str(health),
            "--out",
            str(tmp_path / "gaps.json"),
            "--base-dir",
            str(tmp_path),
        ],
        clock=lambda: NOW,
    ) == 1
    captured = capsys.readouterr()
    assert "absolute-path-do-not-leak" not in captured.err
    assert str(tmp_path.resolve()) not in captured.err
