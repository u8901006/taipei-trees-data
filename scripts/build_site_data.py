"""Build a compact, deterministic browser search index from normalized tree data."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
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
from scripts.coordinates import twd97_to_wgs84
from scripts.match_pruning import match_schedules


PUBLIC_FIELDS = {
    "public_id": "id",
    "tree_type": "tree_type",
    "district": "district",
    "location": "location",
    "park_name": "park_name",
    "species": "species",
    "diameter_cm": "diameter",
    "height_m": "height",
    "updated_at": "updated",
}


def _json_bytes(value: object) -> bytes:
    return (
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        + "\n"
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
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


def _district_value(value: object) -> str:
    public_value = _public_value(value)
    if public_value is None:
        return "行政區未提供"
    cleaned = str(public_value).strip()
    return cleaned or "行政區未提供"


def _stable_district_file(district: str) -> str:
    digest = hashlib.sha256(district.encode("utf-8")).hexdigest()[:16]
    return f"districts/{digest}.json"


def _validate_frame(frame: pd.DataFrame) -> None:
    if list(frame.columns) != CANONICAL_COLUMNS:
        raise ValueError("Parquet 欄位必須完全符合 canonical 欄位")


def _read_frame(path: Path) -> pd.DataFrame:
    try:
        frame = pd.read_parquet(path)
    except Exception as error:
        raise ValueError("無法讀取網站資料來源") from error
    _validate_frame(frame)
    return frame


def _read_schedule_document(path: Path | None) -> dict[str, object]:
    if path is None:
        return {"schema_version": 1, "retrieved_at": None, "schedules": []}
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError) as error:
        raise ValueError("schedule document is invalid") from error
    if (
        not isinstance(document, dict)
        or document.get("schema_version") != 1
        or not isinstance(document.get("schedules"), list)
        or not (
            document.get("retrieved_at") is None
            or isinstance(document.get("retrieved_at"), str)
        )
    ):
        raise ValueError("schedule document is invalid")
    seen: set[str] = set()
    for item in document["schedules"]:
        if (
            not isinstance(item, dict)
            or not isinstance(item.get("schedule_id"), str)
            or item.get("category") not in {"street", "park"}
            or not isinstance(item.get("districts"), list)
            or not isinstance(item.get("locations"), list)
            or not isinstance(item.get("start_date"), str)
            or item["schedule_id"] in seen
        ):
            raise ValueError("schedule document is invalid")
        seen.add(item["schedule_id"])
    return document


def _prepare_frame(street_frame: pd.DataFrame, park_frame: pd.DataFrame | None) -> pd.DataFrame:
    frames = [street_frame.copy()]
    if park_frame is not None:
        frames.append(park_frame.copy())
    frame = pd.concat(frames, ignore_index=True)
    inferred_type = frame["source"].map(
        lambda source: "park" if source == "park_trees" else "street"
    )
    frame["tree_type"] = frame["tree_type"].where(frame["tree_type"].isin(["street", "park"]), inferred_type)
    frame["public_id"] = frame.apply(
        lambda row: f"{row['tree_type']}:{row['tree_id']}", axis=1
    )
    if frame["public_id"].duplicated().any():
        raise ValueError("public tree id must be unique")
    return frame


def _write_index(
    frame: pd.DataFrame, output_dir: Path, schedule_document: dict[str, object]
) -> dict[str, object]:
    districts_dir = output_dir / "districts"
    districts_dir.mkdir(parents=True, exist_ok=True)
    district_entries: list[dict[str, object]] = []

    indexed_frame = frame.copy()
    indexed_frame["district"] = indexed_frame["district"].map(_district_value)
    tree_inputs = [
        {
            "tree_id": str(row["public_id"]),
            "tree_type": row["tree_type"],
            "district": row["district"],
            "location": _public_value(row["location"]),
            "park_name": _public_value(row["park_name"]),
        }
        for row in indexed_frame.to_dict(orient="records")
    ]
    schedules = schedule_document["schedules"]
    assert isinstance(schedules, list)
    matches = match_schedules(tree_inputs, schedules)
    schedule_ids_by_tree: dict[str, list[str]] = {}
    for match in matches:
        schedule_ids_by_tree.setdefault(match["tree_id"], []).append(match["schedule_id"])
    normalized_districts = sorted(set(indexed_frame["district"].tolist()))
    for district in normalized_districts:
        district_frame = indexed_frame[indexed_frame["district"] == district].copy()
        district_frame.sort_values(
            ["public_id", "location", "species"], inplace=True, na_position="last"
        )
        records: list[dict[str, object | None]] = []
        for row in district_frame.to_dict(orient="records"):
            record = {
                public_name: _public_value(row[source_name])
                for source_name, public_name in PUBLIC_FIELDS.items()
            }
            coordinates = twd97_to_wgs84(row["twd97_x"], row["twd97_y"])
            record["latitude"] = coordinates[0] if coordinates else None
            record["longitude"] = coordinates[1] if coordinates else None
            record["schedule_ids"] = schedule_ids_by_tree.get(str(row["public_id"]), [])
            records.append(record)
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
        "schema_version": 2,
        "total_count": int(len(frame)),
        "district_count": len(district_entries),
        "type_counts": {
            "park": int((frame["tree_type"] == "park").sum()),
            "street": int((frame["tree_type"] == "street").sum()),
        },
        "latest_update": update_values[-1] if update_values else None,
        "snapshot_date": snapshot_values[-1] if snapshot_values else None,
        "schedule_count": len(schedules),
        "schedule_retrieved_at": schedule_document.get("retrieved_at"),
        "schedule_file": "schedules.json",
        "schedule_matches_file": "schedule_matches.json",
        "districts": district_entries,
    }
    (output_dir / "schedules.json").write_bytes(_json_bytes(schedule_document))
    (output_dir / "schedule_matches.json").write_bytes(
        _json_bytes({"schema_version": 1, "matches": matches})
    )
    (output_dir / "manifest.json").write_bytes(_json_bytes(manifest))
    return manifest


def build_site_data(
    parquet_path: Path,
    output_dir: Path,
    park_parquet_path: Path | None = None,
    schedule_path: Path | None = None,
) -> dict[str, object]:
    """Validate canonical data and replace the complete public search index."""
    street_frame = _read_frame(parquet_path)
    park_frame = _read_frame(park_parquet_path) if park_parquet_path is not None else None
    frame = _prepare_frame(street_frame, park_frame)
    schedule_document = _read_schedule_document(schedule_path)

    output_dir.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f".{output_dir.name}.", dir=output_dir.parent))
    try:
        manifest = _write_index(frame, staging, schedule_document)
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
    parser.add_argument("--park-src", type=Path)
    parser.add_argument("--schedule", type=Path)
    parser.add_argument("--out", type=Path, required=True)
    arguments = parser.parse_args(argv)
    try:
        manifest = build_site_data(
            arguments.src,
            arguments.out,
            arguments.park_src,
            arguments.schedule,
        )
    except Exception:
        print("網站搜尋索引建立失敗。")
        return 1
    print(f"建立 {manifest['total_count']} 筆、{manifest['district_count']} 個行政區的搜尋索引。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
