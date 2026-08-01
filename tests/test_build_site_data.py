from __future__ import annotations

import json
import math
from pathlib import Path

import pandas as pd
import pytest

from scripts.build_site_data import build_site_data
from scripts.normalize import CANONICAL_COLUMNS


def canonical_frame() -> pd.DataFrame:
    rows = [
        {
            "tree_id": "T-002",
            "tree_type": "street",
            "district": "信義區",
            "location": "松仁路",
            "location_note": "",
            "park_name": None,
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
            "tree_type": "street",
            "district": "大安區",
            "location": "仁愛路",
            "location_note": None,
            "park_name": None,
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


def protected_frame() -> pd.DataFrame:
    row = {column: None for column in CANONICAL_COLUMNS}
    row.update(
        {
            "tree_id": "668",
            "tree_type": "protected",
            "district": None,
            "location": "信義路四段107-1號旁停車場",
            "species": "榕",
            "diameter_cm": 97.0,
            "height_m": 12.4,
            "updated_at": "2026-07-23",
            "scientific_name": "Ficus microcarpa L. f.",
            "english_name": "Banyan",
            "management_unit": "財團法人台灣郵政協會",
            "latitude": 25.033478,
            "longitude": 121.547514,
            "source": "protected_trees",
            "snapshot_date": "2026-08-02",
        }
    )
    return pd.DataFrame([row], columns=CANONICAL_COLUMNS)


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

    assert manifest["schema_version"] == 3
    assert manifest["total_count"] == 2
    assert manifest["type_counts"] == {"park": 0, "protected": 0, "street": 2}
    assert manifest["schedule_count"] == 0
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
    assert {record["id"] for record in records} == {"street:T-001", "street:T-002"}
    assert all(
        set(record)
        == {
            "id",
            "tree_type",
            "district",
            "location",
            "park_name",
            "species",
            "diameter",
            "height",
            "updated",
            "latitude",
            "longitude",
            "schedule_ids",
            "scientific_name",
            "english_name",
            "management_unit",
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
        }
        for record in records
    )
    assert next(record for record in records if record["id"] == "street:T-001")["diameter"] is None
    assert json.loads((output / "schedules.json").read_text(encoding="utf-8"))["schedules"] == []
    assert (
        json.loads((output / "schedule_matches.json").read_text(encoding="utf-8"))["matches"] == []
    )


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
        "schema_version": 3,
        "total_count": 0,
        "district_count": 0,
        "type_counts": {"park": 0, "protected": 0, "street": 0},
        "latest_update": None,
        "snapshot_date": None,
        "schedule_count": 0,
        "schedule_retrieved_at": None,
        "schedule_file": "schedules.json",
        "schedule_matches_file": "schedule_matches.json",
        "species_profile_file": "species_profiles.json",
        "species_profile_count": 0,
        "protected_detail_coverage": {
            "total": 0,
            "available": 0,
            "pending": 0,
            "with_age": 0,
            "with_photo": 0,
            "with_story": 0,
        },
        "districts": [],
    }
    assert list((output / "districts").glob("*.json")) == []


def test_build_site_data_rejects_noncanonical_parquet(tmp_path: Path) -> None:
    parquet = tmp_path / "invalid.parquet"
    canonical_frame().drop(columns=["source"]).to_parquet(parquet, index=False)

    with pytest.raises(ValueError, match="canonical"):
        build_site_data(parquet, tmp_path / "out")


@pytest.mark.parametrize("missing_district", [None, "", "   "])
def test_build_site_data_keeps_missing_district_rows_searchable(
    tmp_path: Path, missing_district: str | None
) -> None:
    frame = canonical_frame().iloc[[0]].copy()
    frame.loc[frame.index[0], "district"] = missing_district
    output = tmp_path / "site-data"

    manifest = build_site_data(write_parquet(tmp_path / "trees.parquet", frame), output)

    assert manifest["total_count"] == 1
    assert manifest["district_count"] == 1
    entry = manifest["districts"][0]
    assert entry["name"] == "行政區未提供"
    records = json.loads((output / entry["file"]).read_text(encoding="utf-8"))
    assert records[0]["district"] == "行政區未提供"


@pytest.mark.parametrize("value", [math.inf, -math.inf])
def test_build_site_data_serializes_non_finite_measurements_as_null(
    tmp_path: Path, value: float
) -> None:
    frame = canonical_frame().iloc[[0]].copy()
    frame.loc[frame.index[0], "diameter_cm"] = value
    output = tmp_path / "site-data"

    manifest = build_site_data(write_parquet(tmp_path / "trees.parquet", frame), output)

    raw = (output / manifest["districts"][0]["file"]).read_text(encoding="utf-8")
    assert "Infinity" not in raw
    assert json.loads(raw)[0]["diameter"] is None


def test_build_site_data_combines_park_maps_and_schedule_candidates(tmp_path: Path) -> None:
    street = canonical_frame().iloc[[0]].copy()
    street.loc[street.index[0], ["tree_id", "district", "location", "twd97_x", "twd97_y"]] = [
        "S-1",
        "松山區",
        "民生東路四段 100 號前",
        306894.85,
        2770248.38,
    ]
    park = canonical_frame().iloc[[0]].copy()
    park.loc[
        park.index[0], ["tree_id", "tree_type", "district", "location", "park_name", "source"]
    ] = [
        "P-1",
        "park",
        "大安區",
        "大安森林公園",
        "大安森林公園",
        "park_trees",
    ]
    street_path = write_parquet(tmp_path / "trees.parquet", street)
    park_path = write_parquet(tmp_path / "park_trees.parquet", park)
    schedule_path = tmp_path / "pruning_schedule.json"
    schedule_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "retrieved_at": "2026-08-01T00:00:00+00:00",
                "schedules": [
                    {
                        "schedule_id": "schedule-1",
                        "category": "street",
                        "districts": ["松山區"],
                        "locations": ["民生東路四段"],
                        "start_date": "2026-08-02",
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    output = tmp_path / "site-data"
    manifest = build_site_data(street_path, output, park_path, schedule_path)

    assert manifest["type_counts"] == {"park": 1, "protected": 0, "street": 1}
    assert manifest["schedule_count"] == 1
    records = [
        record
        for entry in manifest["districts"]
        for record in json.loads((output / entry["file"]).read_text(encoding="utf-8"))
    ]
    street_record = next(record for record in records if record["id"] == "street:S-1")
    assert street_record["latitude"] == pytest.approx(25.0392944, abs=0.0000001)
    assert street_record["longitude"] == pytest.approx(121.5638238, abs=0.0000001)
    assert street_record["schedule_ids"] == ["schedule-1"]
    park_record = next(record for record in records if record["id"] == "park:P-1")
    assert park_record["park_name"] == "大安森林公園"


def test_build_includes_protected_tree_and_species_profile(tmp_path: Path) -> None:
    street = canonical_frame()
    protected = protected_frame()
    details_path = tmp_path / "protected_tree_details.json"
    details_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "records": {
                    "668": {
                        "code": "668",
                        "district": "大安區",
                        "village": "德安里",
                        "age_years": 55,
                        "born_year": 1971,
                        "photo_url": "https://ecultureuser.gov.taipei/upload/image/668.jpg",
                        "photo_count": 2,
                        "story": "樹木的歷史故事。",
                        "environment_description": "樹木生長環境。",
                        "official_detail_url": "https://eculture.gov.taipei/trees/zh-tw/tree/668",
                        "official_modified_at": "2026-07-23T16:52:21",
                        "detail_status": "available",
                        "detail_fetched_at": "2026-08-02T00:00:00+00:00",
                    }
                },
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    output = tmp_path / "data"

    manifest = build_site_data(
        write_parquet(tmp_path / "trees.parquet", street),
        output,
        protected_parquet_path=write_parquet(
            tmp_path / "protected_trees.parquet", protected
        ),
        protected_details_path=details_path,
    )

    assert manifest["type_counts"] == {"park": 0, "protected": 1, "street": 2}
    assert manifest["protected_detail_coverage"] == {
        "total": 1,
        "available": 1,
        "pending": 0,
        "with_age": 1,
        "with_photo": 1,
        "with_story": 1,
    }
    records = [
        record
        for entry in manifest["districts"]
        for record in json.loads((output / entry["file"]).read_text(encoding="utf-8"))
    ]
    protected_record = next(record for record in records if record["id"] == "protected:668")
    assert protected_record["district"] == "大安區"
    assert protected_record["village"] == "德安里"
    assert protected_record["age_years"] == 55
    assert protected_record["age_source"] == "official_protected_tree_registry"
    assert protected_record["latitude"] == pytest.approx(25.033478)
    assert protected_record["longitude"] == pytest.approx(121.547514)
    profiles = json.loads((output / "species_profiles.json").read_text(encoding="utf-8"))
    profile = next(item for item in profiles["profiles"] if item["species"] == "榕")
    assert profile["tree_count"] == 1
    assert profile["scientific_name"] == "Ficus microcarpa L. f."
    assert profile["english_name"] == "Banyan"
    assert profile["average_diameter_cm"] == 97.0
    assert profile["average_height_m"] == 12.4
    assert profile["protected_tree_count"] == 1
    assert all(link["url"].startswith("https://") for link in profile["authoritative_links"])


def test_invalid_schedule_does_not_replace_existing_output(tmp_path: Path) -> None:
    parquet = write_parquet(tmp_path / "trees.parquet")
    output = tmp_path / "site-data"
    output.mkdir()
    marker = output / "manifest.json"
    marker.write_text('{"last":"valid"}\n', encoding="utf-8")
    invalid_schedule = tmp_path / "schedule.json"
    invalid_schedule.write_text('{"schema_version":99}\n', encoding="utf-8")

    with pytest.raises(ValueError, match="schedule"):
        build_site_data(parquet, output, schedule_path=invalid_schedule)

    assert marker.read_text(encoding="utf-8") == '{"last":"valid"}\n'
