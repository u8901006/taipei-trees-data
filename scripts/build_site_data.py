"""Build a compact, deterministic browser search index from normalized tree data."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Sequence

import pandas as pd

if __package__ in {None, ""}:  # Support ``python scripts/build_site_data.py``.
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.normalize import CANONICAL_COLUMNS


PUBLIC_FIELDS = {
    "tree_id": "id",
    "district": "district",
    "location": "location",
    "species": "species",
    "diameter_cm": "diameter",
    "height_m": "height",
    "updated_at": "updated",
}


def _json_bytes(value: object) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode("utf-8")


def _public_value(value: object) -> object | None:
    if value is None:
        return None
    try:
        missing = pd.isna(value)
    except (TypeError, ValueError):
        missing = False
    if not hasattr(missing, "__len__") and bool(missing):
        return None
    if isinstance(value, pd.Timestamp):
        return value.isoformat()
    if hasattr(value, "item"):
        value = value.item()
    return value


def _stable_district_file(district: str) -> str:
    digest = hashlib.sha256(district.encode("utf-8")).hexdigest()[:16]
    return f"districts/{digest}.json"


def _validate_frame(frame: pd.DataFrame) -> None:
    if list(frame.columns) != CANONICAL_COLUMNS:
        raise ValueError("Parquet 欄位必須完全符合 canonical 13 欄")


def _write_index(frame: pd.DataFrame, output_dir: Path) -> dict[str, object]:
    districts_dir = output_dir / "districts"
    districts_dir.mkdir(parents=True, exist_ok=True)
    district_entries: list[dict[str, object]] = []

    normalized_districts = sorted(
        {str(value).strip() for value in frame["district"].dropna().tolist() if str(value).strip()}
    )
    for district in normalized_districts:
        district_frame = frame[frame["district"].astype(str).str.strip() == district].copy()
        district_frame.sort_values(
            ["tree_id", "location", "species"], inplace=True, na_position="last"
        )
        records = [
            {
                public_name: _public_value(row[source_name])
                for source_name, public_name in PUBLIC_FIELDS.items()
            }
            for row in district_frame.to_dict(orient="records")
        ]
        relative_file = _stable_district_file(district)
        (output_dir / relative_file).write_bytes(_json_bytes(records))
        district_entries.append({"name": district, "count": len(records), "file": relative_file})

    update_values = sorted(
        str(value) for value in frame["updated_at"].dropna().tolist() if str(value).strip()
    )
    snapshot_values = sorted(
        str(value) for value in frame["snapshot_date"].dropna().tolist() if str(value).strip()
    )
    manifest: dict[str, object] = {
        "schema_version": 1,
        "total_count": int(len(frame)),
        "district_count": len(district_entries),
        "latest_update": update_values[-1] if update_values else None,
        "snapshot_date": snapshot_values[-1] if snapshot_values else None,
        "districts": district_entries,
    }
    (output_dir / "manifest.json").write_bytes(_json_bytes(manifest))
    return manifest


def build_site_data(parquet_path: Path, output_dir: Path) -> dict[str, object]:
    """Validate canonical data and replace the complete public search index."""
    try:
        frame = pd.read_parquet(parquet_path)
    except Exception as error:
        raise ValueError("無法讀取網站資料來源") from error
    _validate_frame(frame)

    output_dir.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f".{output_dir.name}.", dir=output_dir.parent))
    try:
        manifest = _write_index(frame, staging)
        if output_dir.exists():
            shutil.rmtree(output_dir)
        os.replace(staging, output_dir)
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return manifest


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--src", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    arguments = parser.parse_args(argv)
    try:
        manifest = build_site_data(arguments.src, arguments.out)
    except Exception:
        print("網站搜尋索引建立失敗。")
        return 1
    print(f"建立 {manifest['total_count']} 筆、{manifest['district_count']} 個行政區的搜尋索引。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
