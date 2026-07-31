from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from scripts.gap_report import GapInputError, STALE_AFTER_DAYS, build_gap_report, main


NOW = datetime(2026, 7, 31, 12, 0, tzinfo=UTC)


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
    snapshot.write_bytes(b"gzip snapshot")
    manifest = snapshot.with_suffix("").with_suffix(".json")
    manifest.write_text(
        json.dumps(
            {
                "source_name": source,
                "dataset_id": "dataset-id",
                "resource_id": "resource-id",
                "original_url": "https://data.taipei/download.csv",
                "retrieved_at": f"{day}T01:00:00+00:00",
                "uncompressed_byte_length": 8,
                "sha256": "a" * 64,
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
                "sha256": "b" * 64,
                "byte_length": snapshot.stat().st_size,
                "content_type": "text/csv",
                "retrieved_at": f"{day}T02:00:00+00:00",
            }
        ),
        encoding="utf-8",
    )
    return snapshot, manifest


def write_review_pdf(base: Path, day: str) -> tuple[Path, Path]:
    pdf = base / "raw" / "review_meetings" / day[:7] / "record.pdf"
    pdf.parent.mkdir(parents=True, exist_ok=True)
    pdf.write_bytes(b"%PDF-1.7 fixture")
    manifest = pdf.with_suffix(".manifest.json")
    manifest.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "title": "審議紀錄",
                "published_date": day,
                "detail_url": "https://culture.gov.taipei/detail",
                "attachment_url": "https://culture.gov.taipei/record.pdf",
                "sha256": "c" * 64,
                "byte_length": pdf.stat().st_size,
                "retrieved_at": f"{day}T03:00:00+00:00",
            }
        ),
        encoding="utf-8",
    )
    return pdf, manifest


def gap_codes(report: dict[str, object]) -> list[str]:
    return [gap["code"] for gap in report["gaps"]]  # type: ignore[index]


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


def test_pending_extractions_and_failure_history_report_counts_without_content_leakage(
    tmp_path: Path,
) -> None:
    health = health_document([health_source("review_records", "available")])
    extracted = tmp_path / "extracted"
    extracted.mkdir()
    for name in ("z.json", "a.json"):
        (extracted / name).write_text(
            json.dumps(
                {
                    "schema_version": "1.0",
                    "source_pdf": f"{name}.pdf",
                    "source_sha256": "d" * 64,
                    "model": "SENTINEL-RAW-MODEL",
                    "review_status": "pending",
                    "fields": {"page_text": "SENTINEL-FULL-PAGE"},
                }
            ),
            encoding="utf-8",
        )
    (extracted / "extraction_failures.json").write_text(
        json.dumps(
            {
                "schema_version": "1.0",
                "generated_at": NOW.isoformat(),
                "failures": [
                    {
                        "source_pdf": "secret/source.pdf",
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
    assert "SENTINEL-FULL-PAGE" not in serialized
    assert "secret/source.pdf" not in serialized


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
