from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import pytest

from scripts.build_site_data import build_site_data
from scripts.normalize import CANONICAL_COLUMNS


def canonical_frame() -> pd.DataFrame:
    rows = [
        {
            "tree_id": "T-002",
            "district": "信義區",
            "location": "松仁路",
            "location_note": "",
            "species": "樟樹",
            "diameter_cm": 31.5,
            "height_m": 8.0,
            "survey_date": "2026-07-01",
            "twd97_x": 307000.0,
            "twd97_y": 2770000.0,
            "updated_at": "2026-07-30",
            "source": "street_trees",
            "snapshot_date": "2026-07-31",
        },
        {
            "tree_id": "T-001",
            "district": "大安區",
            "location": "仁愛路",
            "location_note": None,
            "species": "榕樹",
            "diameter_cm": None,
            "height_m": 6.2,
            "survey_date": "2026-06-01",
            "twd97_x": 306000.0,
            "twd97_y": 2769000.0,
            "updated_at": None,
            "source": "street_trees",
            "snapshot_date": "2026-07-31",
        },
    ]
    return pd.DataFrame(rows, columns=CANONICAL_COLUMNS)


def write_parquet(path: Path, frame: pd.DataFrame | None = None) -> Path:
    (frame if frame is not None else canonical_frame()).to_parquet(path, index=False)
    return path


def snapshot_bytes(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*.json"))
    }


def test_build_site_data_splits_districts_and_exposes_only_public_fields(tmp_path: Path) -> None:
    parquet = write_parquet(tmp_path / "trees.parquet")
    output = tmp_path / "site-data"

    manifest = build_site_data(parquet, output)

    assert manifest["schema_version"] == 1
    assert manifest["total_count"] == 2
    assert manifest["latest_update"] == "2026-07-30"
    assert [item["name"] for item in manifest["districts"]] == ["信義區", "大安區"]
    assert sum(item["count"] for item in manifest["districts"]) == 2
    assert json.loads((output / "manifest.json").read_text(encoding="utf-8")) == manifest
    filenames = [item["file"] for item in manifest["districts"]]
    assert len(set(filenames)) == 2
    assert all(
        filename.startswith("districts/") and filename.endswith(".json") for filename in filenames
    )
    records = [
        record
        for item in manifest["districts"]
        for record in json.loads((output / item["file"]).read_text(encoding="utf-8"))
    ]
    assert {record["id"] for record in records} == {"T-001", "T-002"}
    assert all(
        set(record) == {"id", "district", "location", "species", "diameter", "height", "updated"}
        for record in records
    )
    assert next(record for record in records if record["id"] == "T-001")["diameter"] is None


def test_build_site_data_is_byte_stable_for_the_same_rows(tmp_path: Path) -> None:
    parquet = write_parquet(tmp_path / "trees.parquet")
    first = tmp_path / "first"
    second = tmp_path / "second"

    build_site_data(parquet, first)
    build_site_data(parquet, second)

    assert snapshot_bytes(first) == snapshot_bytes(second)


def test_build_site_data_writes_an_empty_search_contract(tmp_path: Path) -> None:
    parquet = write_parquet(tmp_path / "trees.parquet", canonical_frame().iloc[0:0])
    output = tmp_path / "empty"

    manifest = build_site_data(parquet, output)

    assert manifest == {
        "schema_version": 1,
        "total_count": 0,
        "district_count": 0,
        "latest_update": None,
        "snapshot_date": None,
        "districts": [],
    }
    assert list((output / "districts").glob("*.json")) == []


def test_build_site_data_rejects_noncanonical_parquet(tmp_path: Path) -> None:
    parquet = tmp_path / "invalid.parquet"
    canonical_frame().drop(columns=["source"]).to_parquet(parquet, index=False)

    with pytest.raises(ValueError, match="canonical"):
        build_site_data(parquet, tmp_path / "out")
