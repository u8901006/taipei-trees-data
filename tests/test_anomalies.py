from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pandas as pd
import pytest

from scripts.detect_anomalies import detect_anomalies, main


def _snapshot(
    processed: Path,
    source: str,
    when: str,
    tree_ids: list[str],
    sha256: str,
    headers: list[str] | None = None,
) -> None:
    directory = processed / "snapshots" / source
    directory.mkdir(parents=True, exist_ok=True)
    pd.DataFrame({"tree_id": tree_ids}).to_parquet(directory / f"{when}.parquet", index=False)
    (directory / f"{when}.schema.json").write_text(
        json.dumps(
            {
                "original_headers": headers or ["TreeID"],
                "canonical_headers": ["tree_id"],
                "encoding": "utf-8-sig",
                "row_count": len(tree_ids),
                "sha256": sha256,
            }
        ),
        encoding="utf-8",
    )


def test_count_drop_at_half_percent_is_not_an_anomaly(tmp_path: Path) -> None:
    _snapshot(tmp_path, "street_trees", "2025-01-01", [f"T-{n}" for n in range(200)], "one")
    _snapshot(tmp_path, "street_trees", "2025-01-02", [f"T-{n}" for n in range(199)], "two")

    report = detect_anomalies(tmp_path, datetime(2025, 1, 3, tzinfo=UTC))

    assert not [item for item in report.anomalies if item["rule"] == "count_drop"]


def test_larger_drop_and_missing_ids_create_inferred_event(tmp_path: Path) -> None:
    _snapshot(tmp_path, "street_trees", "2025-01-01", ["T-1", "T-2", "T-3"], "one")
    _snapshot(tmp_path, "street_trees", "2025-01-02", ["T-1", "T-2"], "two")

    report = detect_anomalies(tmp_path, datetime(2025, 1, 3, tzinfo=UTC))

    assert any(
        item["severity"] == "high" and item["rule"] == "count_drop" for item in report.anomalies
    )
    missing = next(item for item in report.anomalies if item["rule"] == "missing_tree")
    assert missing["severity"] == "high"
    event = json.loads((tmp_path / "tree_events.jsonl").read_text(encoding="utf-8"))
    assert event == {
        "confidence": "inferred",
        "current_snapshot_date": "2025-01-02",
        "event_type": "removal",
        "previous_snapshot_date": "2025-01-01",
        "source": "street_trees",
        "tree_id": "T-3",
    }


def test_protected_missing_tree_is_critical_and_requires_verification(tmp_path: Path) -> None:
    _snapshot(tmp_path, "protected_trees", "2025-01-01", ["P-1"], "one")
    _snapshot(tmp_path, "protected_trees", "2025-01-02", [], "two")

    report = detect_anomalies(tmp_path, datetime(2025, 1, 3, tzinfo=UTC))

    item = next(item for item in report.anomalies if item["rule"] == "missing_tree")
    assert item["severity"] == "critical"
    assert "需查證" in item["detail"]
    assert "已移除" not in item["detail"]


def test_schema_change_and_three_repeated_hashes_are_reported(tmp_path: Path) -> None:
    _snapshot(tmp_path, "street_trees", "2025-01-01", ["T-1"], "same")
    _snapshot(tmp_path, "street_trees", "2025-01-02", ["T-1"], "same", ["TreeID", "Dist"])
    _snapshot(tmp_path, "street_trees", "2025-01-03", ["T-1"], "same")

    report = detect_anomalies(tmp_path, datetime(2025, 1, 4, tzinfo=UTC))

    assert {(item["severity"], item["rule"]) for item in report.anomalies} == {
        ("medium", "schema_change"),
        ("low", "repeated_raw_hash"),
    }


def test_one_snapshot_is_empty_report_and_cli_sets_github_output(
    tmp_path: Path, monkeypatch
) -> None:
    _snapshot(tmp_path / "processed", "street_trees", "2025-01-01", ["T-1"], "one")
    output = tmp_path / "github-output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))

    assert (
        main(["--processed", str(tmp_path / "processed"), "--out", str(tmp_path / "report.json")])
        == 0
    )

    report = json.loads((tmp_path / "report.json").read_text(encoding="utf-8"))
    assert report["found"] is False
    assert report["anomalies"] == []
    assert output.read_text(encoding="utf-8") == "found=false\n"


@pytest.mark.parametrize("filename", ["20250731", "2025-W31-4", "2025-7-31"])
def test_detect_rejects_non_exact_processed_snapshot_date_filenames(
    tmp_path: Path, filename: str
) -> None:
    _snapshot(tmp_path, "street_trees", filename, ["T-1"], "one")

    with pytest.raises(ValueError, match="YYYY-MM-DD"):
        detect_anomalies(tmp_path)


@pytest.mark.parametrize(
    "line",
    [
        json.dumps(
            {
                "event_type": "removal",
                "confidence": "confirmed",
                "tree_id": "T-1",
                "source": "street_trees",
                "previous_snapshot_date": "2025-01-01",
                "current_snapshot_date": "2025-01-02",
            }
        ),
        "{not-json}",
        json.dumps(
            {
                "event_type": "removal",
                "confidence": "inferred",
                "tree_id": "T-1",
                "source": "street_trees",
                "previous_snapshot_date": "20250101",
                "current_snapshot_date": "2025-01-02",
            }
        ),
    ],
    ids=["confirmed", "malformed", "invalid-date"],
)
def test_detect_rejects_invalid_existing_event_rows_without_echoing_content(
    tmp_path: Path, line: str
) -> None:
    events_path = tmp_path / "tree_events.jsonl"
    events_path.write_text(line + "\n", encoding="utf-8")

    with pytest.raises(ValueError, match="existing tree event") as error:
        detect_anomalies(tmp_path)

    assert line not in str(error.value)


def test_events_are_stably_sorted_and_deduplicated_across_reruns(tmp_path: Path) -> None:
    _snapshot(tmp_path, "street_trees", "2025-01-01", ["T-3", "T-1", "T-2"], "one")
    _snapshot(tmp_path, "street_trees", "2025-01-02", [], "two")

    detect_anomalies(tmp_path, datetime(2025, 1, 3, tzinfo=UTC))
    events_path = tmp_path / "tree_events.jsonl"
    first = events_path.read_text(encoding="utf-8")
    detect_anomalies(tmp_path, datetime(2025, 1, 3, tzinfo=UTC))
    second = events_path.read_text(encoding="utf-8")
    events = [json.loads(line) for line in second.splitlines()]

    assert second == first
    assert [event["tree_id"] for event in events] == ["T-1", "T-2", "T-3"]
    assert all(
        event["event_type"] == "removal" and event["confidence"] == "inferred" for event in events
    )
