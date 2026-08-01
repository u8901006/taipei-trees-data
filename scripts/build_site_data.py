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
from urllib.parse import quote

import pandas as pd

if __package__ in {None, ""}:  # Support ``python scripts/build_site_data.py``.
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.normalize import CANONICAL_COLUMNS
from scripts.coordinates import twd97_many_to_wgs84
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
    "scientific_name": "scientific_name",
    "english_name": "english_name",
    "management_unit": "management_unit",
}

DETAIL_FIELDS = (
    "village",
    "age_years",
    "born_year",
    "age_source",
    "photo_url",
    "photo_count",
    "story",
    "environment_description",
    "official_detail_url",
    "official_modified_at",
    "detail_status",
    "detail_fetched_at",
)


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
            document.get("retrieved_at") is None or isinstance(document.get("retrieved_at"), str)
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


def _read_protected_details(path: Path | None) -> dict[str, dict[str, object]]:
    if path is None:
        return {}
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError) as error:
        raise ValueError("protected detail document is invalid") from error
    records = document.get("records") if isinstance(document, dict) else None
    if document.get("schema_version") != 1 or not isinstance(records, dict):
        raise ValueError("protected detail document is invalid")
    parsed: dict[str, dict[str, object]] = {}
    for code, record in records.items():
        if not isinstance(code, str) or not isinstance(record, dict) or record.get("code") != code:
            raise ValueError("protected detail document is invalid")
        parsed[code] = record
    return parsed


def _prepare_frame(
    street_frame: pd.DataFrame,
    park_frame: pd.DataFrame | None,
    protected_frame: pd.DataFrame | None,
    protected_details: dict[str, dict[str, object]],
) -> pd.DataFrame:
    frames = [street_frame.copy()]
    if park_frame is not None:
        frames.append(park_frame.copy())
    if protected_frame is not None:
        frames.append(protected_frame.copy())
    frame = pd.concat(frames, ignore_index=True)
    inferred_type = frame["source"].map(
        lambda source: (
            "park"
            if source == "park_trees"
            else "protected"
            if source == "protected_trees"
            else "street"
        )
    )
    frame["tree_type"] = frame["tree_type"].where(
        frame["tree_type"].isin(["street", "park", "protected"]), inferred_type
    )
    frame["public_id"] = frame.apply(lambda row: f"{row['tree_type']}:{row['tree_id']}", axis=1)
    if frame["public_id"].duplicated().any():
        raise ValueError("public tree id must be unique")
    for field in DETAIL_FIELDS:
        frame[field] = None
    frame["photo_count"] = 0
    frame["detail_status"] = "not_applicable"
    for index in frame.index[frame["tree_type"] == "protected"]:
        code = str(frame.at[index, "tree_id"])
        detail = protected_details.get(code)
        if detail is None:
            detail = {
                "detail_status": "pending",
                "official_detail_url": (
                    "https://eculture.gov.taipei/trees/zh-tw/tree/"
                    f"{quote(code, safe='')}"
                ),
            }
        for field in DETAIL_FIELDS:
            if field == "age_source":
                continue
            if field in detail:
                frame.at[index, field] = detail[field]
        if _public_value(detail.get("district")) is not None:
            frame.at[index, "district"] = detail["district"]
        if detail.get("age_years") is not None or detail.get("born_year") is not None:
            frame.at[index, "age_source"] = "official_protected_tree_registry"
    return frame


def _mean(values: pd.Series) -> float | None:
    numbers = pd.to_numeric(values, errors="coerce")
    numbers = numbers[numbers.map(lambda value: math.isfinite(value) if pd.notna(value) else False)]
    return round(float(numbers.mean()), 1) if not numbers.empty else None


def _species_profiles(frame: pd.DataFrame) -> dict[str, object]:
    profiles: list[dict[str, object]] = []
    usable_mask = frame["species"].map(lambda value: _public_value(value) is not None).astype(bool)
    usable = frame.loc[usable_mask].copy()
    usable["species"] = usable["species"].map(lambda value: str(value).strip())
    for species, group in usable.groupby("species", sort=True):
        districts = group["district"].map(_district_value).value_counts().sort_index()
        location_values = group.apply(
            lambda row: _public_value(row["location"]) or _public_value(row["park_name"]), axis=1
        )
        location_counts = location_values.dropna().astype(str).value_counts()
        common_locations = [
            {"name": name, "count": int(count)}
            for name, count in sorted(
                location_counts.items(), key=lambda item: (-int(item[1]), str(item[0]))
            )[:5]
        ]
        scientific_names = sorted(
            {
                str(value).strip()
                for value in group["scientific_name"].tolist()
                if _public_value(value) is not None and str(value).strip()
            }
        )
        english_names = sorted(
            {
                str(value).strip()
                for value in group["english_name"].tolist()
                if _public_value(value) is not None and str(value).strip()
            }
        )
        encoded = quote(species, safe="")
        profiles.append(
            {
                "species": species,
                "scientific_name": scientific_names[0] if len(scientific_names) == 1 else None,
                "scientific_names": scientific_names,
                "scientific_name_conflict": len(scientific_names) > 1,
                "english_name": english_names[0] if len(english_names) == 1 else None,
                "english_names": english_names,
                "tree_count": int(len(group)),
                "district_counts": {name: int(count) for name, count in districts.items()},
                "common_locations": common_locations,
                "average_diameter_cm": _mean(group["diameter_cm"]),
                "average_height_m": _mean(group["height_m"]),
                "protected_tree_count": int((group["tree_type"] == "protected").sum()),
                "authoritative_links": [
                    {"label": "TAI2 臺灣植物資訊整合查詢", "url": "https://tai2.ntu.edu.tw/"},
                    {
                        "label": "TBN 臺灣生物多樣性網絡",
                        "url": f"https://www.tbn.org.tw/taxa?keyword={encoded}",
                    },
                ],
            }
        )
    updates = sorted(
        str(value) for value in frame["updated_at"].dropna().tolist() if str(value).strip()
    )
    return {
        "schema_version": 1,
        "updated_at": updates[-1] if updates else None,
        "profiles": profiles,
    }


def _write_index(
    frame: pd.DataFrame, output_dir: Path, schedule_document: dict[str, object]
) -> dict[str, object]:
    districts_dir = output_dir / "districts"
    districts_dir.mkdir(parents=True, exist_ok=True)
    district_entries: list[dict[str, object]] = []

    indexed_frame = frame.copy()
    indexed_frame["district"] = indexed_frame["district"].map(_district_value)
    converted_coordinates = twd97_many_to_wgs84(
        indexed_frame["twd97_x"].tolist(), indexed_frame["twd97_y"].tolist()
    )
    source_latitudes = indexed_frame["latitude"].tolist()
    source_longitudes = indexed_frame["longitude"].tolist()
    latitudes: list[object | None] = []
    longitudes: list[object | None] = []
    for tree_type, source_latitude, source_longitude, converted in zip(
        indexed_frame["tree_type"].tolist(),
        source_latitudes,
        source_longitudes,
        converted_coordinates,
        strict=True,
    ):
        if tree_type == "protected" and _public_value(source_latitude) is not None and _public_value(source_longitude) is not None:
            latitudes.append(source_latitude)
            longitudes.append(source_longitude)
        else:
            latitudes.append(converted[0] if converted else None)
            longitudes.append(converted[1] if converted else None)
    indexed_frame["latitude"] = latitudes
    indexed_frame["longitude"] = longitudes
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
            record["latitude"] = _public_value(row["latitude"])
            record["longitude"] = _public_value(row["longitude"])
            record["schedule_ids"] = schedule_ids_by_tree.get(str(row["public_id"]), [])
            for field in DETAIL_FIELDS:
                record[field] = _public_value(row[field])
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
    species_document = _species_profiles(indexed_frame)
    protected = indexed_frame[indexed_frame["tree_type"] == "protected"]
    available = int((protected["detail_status"] == "available").sum())
    pending = int((protected["detail_status"] == "pending").sum())
    coverage = {
        "total": int(len(protected)),
        "available": available,
        "pending": pending,
        "with_age": int(
            (protected["age_years"].notna() | protected["born_year"].notna()).sum()
        ),
        "with_photo": int(protected["photo_url"].notna().sum()),
        "with_story": int(
            (protected["story"].notna() | protected["environment_description"].notna()).sum()
        ),
    }
    manifest: dict[str, object] = {
        "schema_version": 3,
        "total_count": int(len(frame)),
        "district_count": len(district_entries),
        "type_counts": {
            "park": int((frame["tree_type"] == "park").sum()),
            "protected": int((frame["tree_type"] == "protected").sum()),
            "street": int((frame["tree_type"] == "street").sum()),
        },
        "latest_update": update_values[-1] if update_values else None,
        "snapshot_date": snapshot_values[-1] if snapshot_values else None,
        "schedule_count": len(schedules),
        "schedule_retrieved_at": schedule_document.get("retrieved_at"),
        "schedule_file": "schedules.json",
        "schedule_matches_file": "schedule_matches.json",
        "species_profile_file": "species_profiles.json",
        "species_profile_count": len(species_document["profiles"]),
        "protected_detail_coverage": coverage,
        "districts": district_entries,
    }
    (output_dir / "schedules.json").write_bytes(_json_bytes(schedule_document))
    (output_dir / "schedule_matches.json").write_bytes(
        _json_bytes({"schema_version": 1, "matches": matches})
    )
    (output_dir / "species_profiles.json").write_bytes(_json_bytes(species_document))
    (output_dir / "manifest.json").write_bytes(_json_bytes(manifest))
    return manifest


def build_site_data(
    parquet_path: Path,
    output_dir: Path,
    park_parquet_path: Path | None = None,
    schedule_path: Path | None = None,
    protected_parquet_path: Path | None = None,
    protected_details_path: Path | None = None,
) -> dict[str, object]:
    """Validate canonical data and replace the complete public search index."""
    street_frame = _read_frame(parquet_path)
    park_frame = _read_frame(park_parquet_path) if park_parquet_path is not None else None
    protected_frame = (
        _read_frame(protected_parquet_path) if protected_parquet_path is not None else None
    )
    protected_details = _read_protected_details(protected_details_path)
    frame = _prepare_frame(street_frame, park_frame, protected_frame, protected_details)
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
    parser.add_argument("--protected-src", type=Path)
    parser.add_argument("--protected-details", type=Path)
    parser.add_argument("--out", type=Path, required=True)
    arguments = parser.parse_args(argv)
    try:
        manifest = build_site_data(
            arguments.src,
            arguments.out,
            arguments.park_src,
            arguments.schedule,
            arguments.protected_src,
            arguments.protected_details,
        )
    except Exception:
        print("網站搜尋索引建立失敗。")
        return 1
    print(f"建立 {manifest['total_count']} 筆、{manifest['district_count']} 個行政區的搜尋索引。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
