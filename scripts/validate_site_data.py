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
    districts = manifest.get("districts")
    if not isinstance(districts, list):
        raise ValueError("manifest districts must be a list")
    if manifest.get("total_count", 0) < minimum_total:
        raise ValueError("total_count is below the safe deployment threshold")
    if manifest.get("district_count") != expected_districts or len(districts) != expected_districts:
        raise ValueError("district count does not match the deployment contract")

    counted = 0
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
        counted += len(records)
    if counted != manifest["total_count"]:
        raise ValueError("partition count does not match total_count")


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
