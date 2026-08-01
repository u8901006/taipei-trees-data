"""Normalize immutable raw CSV snapshots into deterministic Parquet datasets."""

from __future__ import annotations

import argparse
import csv
import gzip
import hashlib
import json
import re
from dataclasses import dataclass
from datetime import date, datetime
from io import StringIO
from pathlib import Path
from typing import Sequence

import pandas as pd


CANONICAL_COLUMNS = [
    "tree_id",
    "tree_type",
    "district",
    "location",
    "location_note",
    "park_name",
    "species",
    "diameter_cm",
    "height_m",
    "survey_date",
    "twd97_x",
    "twd97_y",
    "updated_at",
    "scientific_name",
    "english_name",
    "management_unit",
    "latitude",
    "longitude",
    "source",
    "snapshot_date",
]

_ALIASES = {
    "tree_id": ("TreeID", "樹籍編號", "編號", "樹木編號"),
    "district": ("Dist", "行政區"),
    "location": ("Region", "路段位置", "地址"),
    "location_note": ("RegionRemark", "路段備註"),
    "park_name": ("ParkName", "公園名稱"),
    "species": ("TreeType", "樹種", "樹種名稱"),
    "diameter_cm": ("Diameter", "胸徑", "樹胸徑寬度公尺"),
    "height_m": ("TreeHeight", "樹高"),
    "survey_date": ("SurveyDate", "調查日期"),
    "twd97_x": ("TWD97X",),
    "twd97_y": ("TWD97Y",),
    "updated_at": ("Update", "UpdDate", "更新日期"),
    "scientific_name": ("樹種學名",),
    "english_name": ("英文名",),
    "management_unit": ("管理單位",),
    "latitude": ("緯度",),
    "longitude": ("經度",),
}
_CANONICAL_BY_ALIAS = {
    alias.strip().casefold(): canonical
    for canonical, aliases in _ALIASES.items()
    for alias in aliases
}
_NUMERIC_COLUMNS = (
    "diameter_cm",
    "height_m",
    "twd97_x",
    "twd97_y",
    "latitude",
    "longitude",
)
_DATE_COLUMNS = ("survey_date", "updated_at")
_SNAPSHOT_DATE_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}$")


@dataclass(frozen=True, slots=True)
class NormalizationMetadata:
    original_headers: list[str]
    canonical_headers: list[str]
    encoding: str
    row_count: int
    sha256: str


@dataclass(frozen=True, slots=True)
class NormalizedSnapshot:
    source: str
    snapshot_date: date
    path: Path
    metadata_path: Path


def decode_csv_bytes(content: bytes) -> tuple[str, str]:
    """Decode official CSV bytes without exposing undecodable source content."""
    for encoding in ("utf-8-sig", "utf-8", "cp950", "big5"):
        try:
            return content.decode(encoding), encoding
        except UnicodeDecodeError:
            continue
    raise ValueError("CSV content could not be decoded with a supported encoding")


def _null_if_blank(value: object) -> str | None:
    if value is None:
        return None
    cleaned = str(value).strip()
    return cleaned or None


def _iso_date(value: object) -> str | None:
    cleaned = _null_if_blank(value)
    if cleaned is None:
        return None
    try:
        return date.fromisoformat(cleaned).isoformat()
    except ValueError:
        try:
            return datetime.fromisoformat(cleaned).isoformat()
        except ValueError:
            return None


def normalize_rows(
    content: bytes, source: str, snapshot_date: date
) -> tuple[pd.DataFrame, NormalizationMetadata]:
    """Normalize one raw CSV snapshot using only declared official aliases."""
    text, encoding = decode_csv_bytes(content)
    reader = csv.DictReader(StringIO(text, newline=""))
    original_headers = list(reader.fieldnames or [])
    header_mapping = [
        _CANONICAL_BY_ALIAS.get(header.strip().casefold()) for header in original_headers
    ]
    canonical_headers = [header for header in header_mapping if header is not None]
    if len(canonical_headers) != len(set(canonical_headers)):
        raise ValueError("CSV has duplicate canonical column aliases")
    if "tree_id" not in canonical_headers:
        raise ValueError("CSV is missing required tree_id column")

    records: list[dict[str, object]] = []
    for row in reader:
        normalized = {column: None for column in CANONICAL_COLUMNS}
        for original, canonical in zip(original_headers, header_mapping, strict=True):
            if canonical is not None:
                normalized[canonical] = _null_if_blank(row.get(original))
        if source == "park_trees":
            normalized["tree_type"] = "park"
            normalized["location"] = normalized["park_name"]
        elif source == "street_trees":
            normalized["tree_type"] = "street"
        elif source == "protected_trees":
            normalized["tree_type"] = "protected"
        normalized["source"] = source
        normalized["snapshot_date"] = snapshot_date.isoformat()
        records.append(normalized)

    identifiers = [record["tree_id"] for record in records]
    if any(identifier is None for identifier in identifiers):
        raise ValueError("tree_id must be non-empty")
    if len(identifiers) != len(set(identifiers)):
        raise ValueError("tree_id must be unique within a source snapshot")

    frame = pd.DataFrame(records, columns=CANONICAL_COLUMNS)
    for column in _NUMERIC_COLUMNS:
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
    if source == "protected_trees":
        frame["diameter_cm"] = frame["diameter_cm"] * 100
    for column in _DATE_COLUMNS:
        frame[column] = frame[column].map(_iso_date)
    frame = frame.sort_values("tree_id", kind="mergesort", ignore_index=True)
    metadata = NormalizationMetadata(
        original_headers=original_headers,
        canonical_headers=canonical_headers,
        encoding=encoding,
        row_count=len(frame),
        sha256=hashlib.sha256(content).hexdigest(),
    )
    return frame, metadata


def _parse_snapshot_date(filename_date: str) -> date:
    if _SNAPSHOT_DATE_PATTERN.fullmatch(filename_date) is None:
        raise ValueError("raw snapshot filename must use YYYY-MM-DD.csv.gz")
    try:
        return date.fromisoformat(filename_date)
    except ValueError as error:
        raise ValueError("raw snapshot filename must use YYYY-MM-DD.csv.gz") from error


def _write_schema(path: Path, metadata: NormalizationMetadata) -> None:
    payload = {
        "canonical_headers": metadata.canonical_headers,
        "encoding": metadata.encoding,
        "original_headers": metadata.original_headers,
        "row_count": metadata.row_count,
        "sha256": metadata.sha256,
    }
    path.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8"
    )


def normalize_all(raw_dir: Path, out_dir: Path) -> list[NormalizedSnapshot]:
    """Rebuild all date-partitioned derived snapshots from immutable raw inputs."""
    snapshots: list[NormalizedSnapshot] = []
    latest_frames: dict[str, tuple[date, pd.DataFrame]] = {}
    for raw_path in sorted(
        raw_dir.glob("*/*.csv.gz"), key=lambda path: (path.parent.name, path.name)
    ):
        snapshot_date = _parse_snapshot_date(raw_path.name.removesuffix(".csv.gz"))
        try:
            content = gzip.decompress(raw_path.read_bytes())
        except OSError as error:
            raise ValueError("raw snapshot is not a valid gzip CSV") from error
        frame, metadata = normalize_rows(content, raw_path.parent.name, snapshot_date)
        snapshot_dir = out_dir / "snapshots" / raw_path.parent.name
        snapshot_dir.mkdir(parents=True, exist_ok=True)
        parquet_path = snapshot_dir / f"{snapshot_date.isoformat()}.parquet"
        frame.to_parquet(parquet_path, index=False, engine="pyarrow", compression="zstd")
        metadata_path = snapshot_dir / f"{snapshot_date.isoformat()}.schema.json"
        _write_schema(metadata_path, metadata)
        snapshots.append(
            NormalizedSnapshot(raw_path.parent.name, snapshot_date, parquet_path, metadata_path)
        )
        previous = latest_frames.get(raw_path.parent.name)
        if previous is None or snapshot_date > previous[0]:
            latest_frames[raw_path.parent.name] = (snapshot_date, frame)

    if "street_trees" in latest_frames:
        latest_frames["street_trees"][1].to_parquet(
            out_dir / "trees.parquet", index=False, engine="pyarrow", compression="zstd"
        )
    if "protected_trees" in latest_frames:
        latest_frames["protected_trees"][1].to_parquet(
            out_dir / "protected_trees.parquet", index=False, engine="pyarrow", compression="zstd"
        )
    if "park_trees" in latest_frames:
        latest_frames["park_trees"][1].to_parquet(
            out_dir / "park_trees.parquet", index=False, engine="pyarrow", compression="zstd"
        )
    return sorted(snapshots, key=lambda item: (item.source, item.snapshot_date))


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--raw", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    arguments = parser.parse_args(argv)
    for snapshot in normalize_all(arguments.raw, arguments.out):
        print(f"{snapshot.source}/{snapshot.snapshot_date.isoformat()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
