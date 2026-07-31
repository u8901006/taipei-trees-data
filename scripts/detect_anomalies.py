"""Detect follow-up signals in normalized public tree-data snapshots."""

from __future__ import annotations

import argparse
import json
import os
import re
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, Sequence

import pandas as pd


_SEVERITY_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3}
_SNAPSHOT_DATE_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_EVENT_FIELDS = frozenset(
    {
        "event_type",
        "confidence",
        "tree_id",
        "source",
        "previous_snapshot_date",
        "current_snapshot_date",
    }
)


@dataclass(frozen=True, slots=True)
class AnomalyReport:
    schema_version: str
    generated_at: str
    found: bool
    summary: str
    detail: str
    anomalies: list[dict[str, object]]

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "generated_at": self.generated_at,
            "found": self.found,
            "summary": self.summary,
            "detail": self.detail,
            "anomalies": self.anomalies,
        }


@dataclass(frozen=True, slots=True)
class _Snapshot:
    source: str
    date_text: str
    frame: pd.DataFrame
    schema: dict[str, Any]


def _safe_schema(path: Path) -> dict[str, Any]:
    try:
        parsed = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise ValueError("normalized schema metadata is invalid") from error
    if not isinstance(parsed, dict):
        raise ValueError("normalized schema metadata is invalid")
    return parsed


def _parse_snapshot_date(date_text: str, error_message: str) -> date:
    if _SNAPSHOT_DATE_PATTERN.fullmatch(date_text) is None:
        raise ValueError(error_message)
    try:
        return date.fromisoformat(date_text)
    except ValueError as error:
        raise ValueError(error_message) from error


def _load_snapshots(processed_dir: Path) -> dict[str, list[_Snapshot]]:
    results: dict[str, list[_Snapshot]] = {}
    root = processed_dir / "snapshots"
    if not root.exists():
        return results
    for parquet_path in sorted(
        root.glob("*/*.parquet"), key=lambda path: (path.parent.name, path.name)
    ):
        date_text = parquet_path.stem
        _parse_snapshot_date(date_text, "normalized snapshot filename must use YYYY-MM-DD.parquet")
        schema_path = parquet_path.with_suffix(".schema.json")
        results.setdefault(parquet_path.parent.name, []).append(
            _Snapshot(
                parquet_path.parent.name,
                date_text,
                pd.read_parquet(parquet_path),
                _safe_schema(schema_path),
            )
        )
    for source in results:
        results[source].sort(key=lambda item: item.date_text)
    return results


def _anomaly(
    severity: str, source: str, rule: str, detail: str, tree_id: str | None = None
) -> dict[str, object]:
    item: dict[str, object] = {
        "severity": severity,
        "source": source,
        "rule": rule,
        "title": "公開樹木資料需查證",
        "detail": detail,
    }
    if tree_id is not None:
        item["tree_id"] = tree_id
    return item


def _event(source: str, previous: _Snapshot, current: _Snapshot, tree_id: str) -> dict[str, str]:
    return {
        "event_type": "removal",
        "confidence": "inferred",
        "tree_id": tree_id,
        "source": source,
        "previous_snapshot_date": previous.date_text,
        "current_snapshot_date": current.date_text,
    }


def _validate_existing_event(item: object) -> dict[str, str]:
    error_message = "existing tree event is invalid"
    if not isinstance(item, dict) or set(item) != _EVENT_FIELDS:
        raise ValueError(error_message)
    if not all(isinstance(value, str) for value in item.values()):
        raise ValueError(error_message)
    event = dict(item)
    if event["event_type"] != "removal" or event["confidence"] != "inferred":
        raise ValueError(error_message)
    if not event["tree_id"].strip() or not event["source"].strip():
        raise ValueError(error_message)
    previous = _parse_snapshot_date(event["previous_snapshot_date"], error_message)
    current = _parse_snapshot_date(event["current_snapshot_date"], error_message)
    if previous >= current:
        raise ValueError(error_message)
    return event


def _write_events(path: Path, events: list[dict[str, str]]) -> None:
    existing: list[dict[str, str]] = []
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                item = json.loads(line)
            except ValueError as error:
                raise ValueError("existing tree event is invalid") from error
            existing.append(_validate_existing_event(item))
    unique = {
        json.dumps(item, ensure_ascii=False, sort_keys=True, separators=(",", ":")): item
        for item in [*existing, *events]
    }
    ordered = sorted(
        unique.values(),
        key=lambda item: (
            item.get("source", ""),
            item.get("tree_id", ""),
            item.get("previous_snapshot_date", ""),
            item.get("current_snapshot_date", ""),
        ),
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(
            json.dumps(item, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
            for item in ordered
        ),
        encoding="utf-8",
    )


def detect_anomalies(processed_dir: Path, now: datetime | None = None) -> AnomalyReport:
    """Return comparison signals; missing IDs are inferred, never confirmed removals."""
    anomalies: list[dict[str, object]] = []
    events: list[dict[str, str]] = []
    for source, snapshots in sorted(_load_snapshots(processed_dir).items()):
        if len(snapshots) >= 2:
            previous, current = snapshots[-2:]
            previous_count, current_count = len(previous.frame), len(current.frame)
            if previous_count and (previous_count - current_count) / previous_count > 0.005:
                anomalies.append(
                    _anomaly(
                        "high",
                        source,
                        "count_drop",
                        "最新快照的資料筆數明顯下降，僅為資料異常訊號，需進一步查證。",
                    )
                )
            previous_ids = {
                str(value) for value in previous.frame.get("tree_id", pd.Series(dtype=str)).dropna()
            }
            current_ids = {
                str(value) for value in current.frame.get("tree_id", pd.Series(dtype=str)).dropna()
            }
            for tree_id in sorted(previous_ids - current_ids):
                severity = "critical" if source == "protected_trees" else "high"
                detail = (
                    "受保護樹木識別碼未出現在最新快照，僅為推測，需查證，並不表示已確認移除。"
                    if severity == "critical"
                    else "樹木識別碼未出現在最新快照，僅為推測，需進一步查證。"
                )
                anomalies.append(_anomaly(severity, source, "missing_tree", detail, tree_id))
                events.append(_event(source, previous, current, tree_id))
            previous_original = set(previous.schema.get("original_headers", []))
            current_original = set(current.schema.get("original_headers", []))
            previous_canonical = set(previous.schema.get("canonical_headers", []))
            current_canonical = set(current.schema.get("canonical_headers", []))
            if previous_original != current_original or previous_canonical != current_canonical:
                anomalies.append(
                    _anomaly(
                        "medium",
                        source,
                        "schema_change",
                        "最新快照的欄位集合有所變動，需檢視資料格式後再使用。",
                    )
                )
        hashes = [str(snapshot.schema.get("sha256", "")) for snapshot in snapshots]
        run_start = 0
        while run_start < len(hashes):
            run_end = run_start + 1
            while run_end < len(hashes) and hashes[run_end] == hashes[run_start]:
                run_end += 1
            if hashes[run_start] and run_end - run_start >= 3:
                anomalies.append(
                    _anomaly(
                        "low",
                        source,
                        "repeated_raw_hash",
                        "連續三個以上快照的原始資料雜湊相同，可能需要確認資料是否更新。",
                    )
                )
            run_start = run_end

    anomalies.sort(
        key=lambda item: (
            _SEVERITY_ORDER[str(item["severity"])],
            str(item["source"]),
            str(item.get("tree_id", item["rule"])),
            str(item["rule"]),
        )
    )
    _write_events(processed_dir / "tree_events.jsonl", events)
    generated_at = (now if now is not None else datetime.now(UTC)).isoformat()
    found = bool(anomalies)
    return AnomalyReport(
        schema_version="1.0",
        generated_at=generated_at,
        found=found,
        summary=(
            "偵測到公開資料異常，請依嚴重程度查證。"
            if found
            else "未偵測到需要比較的公開資料異常。"
        ),
        detail=(
            "此報告僅提供資料品質與追蹤訊號，不代表任何樹木狀態已獲確認。"
            if found
            else "資料快照不足或未發現比較異常。"
        ),
        anomalies=anomalies,
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--processed", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    arguments = parser.parse_args(argv)
    report = detect_anomalies(arguments.processed)
    arguments.out.parent.mkdir(parents=True, exist_ok=True)
    arguments.out.write_text(
        json.dumps(report.to_dict(), ensure_ascii=False, sort_keys=False, indent=2) + "\n",
        encoding="utf-8",
    )
    output_path = os.environ.get("GITHUB_OUTPUT")
    if output_path is not None:
        with Path(output_path).open("a", encoding="utf-8", newline="\n") as output:
            output.write(f"found={str(report.found).lower()}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
