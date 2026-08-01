"""Fail closed before publishing an incomplete public tree search index."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Sequence


def _read_json(path: Path) -> object:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise ValueError(f"invalid JSON: {path.name}") from error


def validate_site_data(data_dir: Path, minimum_total: int, expected_districts: int) -> None:
    """Validate manifest thresholds and every referenced partition before deploy."""
    if minimum_total < 1 or expected_districts < 1:
        raise ValueError("deployment thresholds must be positive")
    manifest = _read_json(data_dir / "manifest.json")
    if not isinstance(manifest, dict):
        raise ValueError("manifest must be an object")
    if manifest.get("schema_version") != 2:
        raise ValueError("manifest schema version is unsupported")
    districts = manifest.get("districts")
    if not isinstance(districts, list):
        raise ValueError("manifest districts must be a list")
    if manifest.get("total_count", 0) < minimum_total:
        raise ValueError("total_count is below the safe deployment threshold")
    if manifest.get("district_count") != expected_districts or len(districts) != expected_districts:
        raise ValueError("district count does not match the deployment contract")

    type_counts = manifest.get("type_counts")
    if (
        not isinstance(type_counts, dict)
        or set(type_counts) != {"street", "park"}
        or any(type(value) is not int or value < 0 for value in type_counts.values())
        or sum(type_counts.values()) != manifest.get("total_count")
    ):
        raise ValueError("tree type counts do not match total_count")

    counted = 0
    tree_ids: set[str] = set()
    for entry in districts:
        if not isinstance(entry, dict) or not isinstance(entry.get("count"), int):
            raise ValueError("district entry is invalid")
        relative = entry.get("file")
        if not isinstance(relative, str):
            raise ValueError("district file is invalid")
        relative_path = Path(relative)
        if (
            relative_path.is_absolute()
            or len(relative_path.parts) != 2
            or relative_path.parts[0] != "districts"
            or relative_path.suffix != ".json"
        ):
            raise ValueError("district file must be a safe relative JSON path")
        records = _read_json(data_dir / relative_path)
        if not isinstance(records, list) or len(records) != entry["count"]:
            raise ValueError("district partition count does not match manifest count")
        if not records:
            raise ValueError("district partition must not be empty")
        for record in records:
            if not isinstance(record, dict) or not isinstance(record.get("id"), str):
                raise ValueError("tree record is invalid")
            if record["id"] in tree_ids:
                raise ValueError("tree id must be unique")
            tree_ids.add(record["id"])
        counted += len(records)
    if counted != manifest["total_count"]:
        raise ValueError("partition count does not match total_count")

    schedule_file = manifest.get("schedule_file")
    matches_file = manifest.get("schedule_matches_file")
    if schedule_file != "schedules.json" or matches_file != "schedule_matches.json":
        raise ValueError("schedule files are invalid")
    schedule_document = _read_json(data_dir / schedule_file)
    if (
        not isinstance(schedule_document, dict)
        or schedule_document.get("schema_version") != 1
        or not isinstance(schedule_document.get("schedules"), list)
    ):
        raise ValueError("schedule document is invalid")
    schedule_ids: set[str] = set()
    for schedule in schedule_document["schedules"]:
        if not isinstance(schedule, dict) or not isinstance(schedule.get("schedule_id"), str):
            raise ValueError("schedule record is invalid")
        if schedule["schedule_id"] in schedule_ids:
            raise ValueError("schedule id must be unique")
        schedule_ids.add(schedule["schedule_id"])
    if manifest.get("schedule_count") != len(schedule_ids):
        raise ValueError("schedule count does not match manifest")

    match_document = _read_json(data_dir / matches_file)
    if (
        not isinstance(match_document, dict)
        or match_document.get("schema_version") != 1
        or not isinstance(match_document.get("matches"), list)
    ):
        raise ValueError("schedule matches are invalid")
    for match in match_document["matches"]:
        if (
            not isinstance(match, dict)
            or match.get("schedule_id") not in schedule_ids
            or match.get("tree_id") not in tree_ids
        ):
            raise ValueError("schedule match references are invalid")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", required=True, type=Path)
    parser.add_argument("--minimum-total", required=True, type=int)
    parser.add_argument("--expected-districts", required=True, type=int)
    arguments = parser.parse_args(argv)
    try:
        validate_site_data(arguments.data, arguments.minimum_total, arguments.expected_districts)
    except ValueError as error:
        print(f"網站資料驗證失敗：{error}", file=sys.stderr)
        return 1
    print("網站資料通過發布安全閘門。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
