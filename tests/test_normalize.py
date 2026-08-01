from __future__ import annotations

import gzip
import hashlib
import json
from datetime import date
from pathlib import Path

import pandas as pd
import pytest

from scripts.normalize import decode_csv_bytes, normalize_all, normalize_rows


def _write_raw(raw_dir: Path, source: str, when: str, content: bytes) -> None:
    path = raw_dir / source / f"{when}.csv.gz"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(gzip.compress(content, mtime=0))
    path.with_suffix("").with_suffix(".json").write_text(
        json.dumps({"sha256": hashlib.sha256(content).hexdigest()}), encoding="utf-8"
    )


def test_decode_csv_bytes_supports_utf8_bom_cp950_and_big5() -> None:
    assert decode_csv_bytes(b"\xef\xbb\xbfTreeID\nA-1\n") == ("TreeID\nA-1\n", "utf-8-sig")
    assert decode_csv_bytes("TreeID,行政區\nA-1,大安區\n".encode("cp950")) == (
        "TreeID,行政區\nA-1,大安區\n",
        "cp950",
    )
    assert (
        decode_csv_bytes("TreeID,樹種\nA-1,榕樹\n".encode("big5"))[0] == "TreeID,樹種\nA-1,榕樹\n"
    )


def test_decode_csv_bytes_hides_undecodable_source_rows() -> None:
    with pytest.raises(ValueError) as error:
        decode_csv_bytes(b"TreeID\nsecret-row-\xff\n")

    assert "secret-row" not in str(error.value)


def test_normalize_aliases_coercion_nulls_and_sort_order() -> None:
    content = (
        "TreeID,Dist,Region,RegionRemark,TreeType,Diameter,TreeHeight,SurveyDate,TWD97X,TWD97Y,UpdDate\n"
        " B-2 , 大安區 ,公園,   ,榕樹,not-a-number, 3.5 ,2025-01-02,121.5,,2025-01-03\n"
        "A-1,中山區,路口,備註,樟樹,12, ,not-a-date,,,\n"
    ).encode()

    frame, metadata = normalize_rows(content, "street_trees", date(2025, 1, 4))

    assert list(frame.columns) == [
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
    assert frame["tree_id"].tolist() == ["A-1", "B-2"]
    assert pd.isna(frame.loc[0, "height_m"])
    assert pd.isna(frame.loc[1, "diameter_cm"])
    assert frame.loc[1, "survey_date"] == "2025-01-02"
    assert pd.isna(frame.loc[0, "survey_date"])
    assert pd.isna(frame.loc[1, "location_note"])
    assert metadata.original_headers[0] == "TreeID"
    assert metadata.canonical_headers[0] == "tree_id"


def test_normalize_park_tree_maps_park_name_and_type() -> None:
    content = (
        "TreeID,Dist,ParkName,TreeType,Diameter,TreeHeight,TWD97X,TWD97Y,SurveyDate,UpdDate\n"
        "P-1,大安區,大安森林公園,榕樹,42,9,304555,2769250,2026-07-01,2026-07-31\n"
    ).encode("utf-8")

    frame, metadata = normalize_rows(content, "park_trees", date(2026, 8, 1))

    assert frame.loc[0, "park_name"] == "大安森林公園"
    assert frame.loc[0, "location"] == "大安森林公園"
    assert frame.loc[0, "tree_type"] == "park"
    assert frame.loc[0, "updated_at"] == "2026-07-31"
    assert "park_name" in metadata.canonical_headers


def test_normalize_protected_tree_official_columns() -> None:
    fixture = Path(__file__).parent / "fixtures" / "protected_trees.csv"

    frame, metadata = normalize_rows(fixture.read_bytes(), "protected_trees", date(2026, 8, 2))

    row = frame.iloc[0]
    assert row["tree_type"] == "protected"
    assert row["tree_id"] == "668"
    assert row["species"] == "榕"
    assert row["scientific_name"] == "Ficus microcarpa L. f."
    assert row["english_name"] == "Banyan"
    assert row["diameter_cm"] == pytest.approx(97.0)
    assert row["latitude"] == pytest.approx(25.033478)
    assert row["longitude"] == pytest.approx(121.547514)
    assert row["management_unit"] == "財團法人台灣郵政協會"
    assert "scientific_name" in metadata.canonical_headers


def test_normalize_protected_tree_infers_district_from_official_address() -> None:
    content = (
        "樹木編號,樹種名稱,地址,緯度,經度\n768,榕,臺北市萬華區騰雲里青年公園,25.0232,121.5056\n"
    ).encode("utf-8-sig")

    frame, _ = normalize_rows(content, "protected_trees", date(2026, 8, 2))

    assert frame.loc[0, "district"] == "萬華區"


@pytest.mark.parametrize(
    ("tree_header", "location_header"),
    [("樹籍編號", "路段位置"), ("編號", "地址")],
)
def test_normalize_real_traditional_chinese_official_aliases(
    tree_header: str, location_header: str
) -> None:
    content = (
        f"{tree_header},行政區,{location_header},路段備註,樹種,胸徑,樹高,調查日期,更新日期\n"
        "T-100,大安區,仁愛路,靠近入口,榕樹,24.5,8.2,2025-01-02,2025-01-03\n"
    ).encode("utf-8")

    frame, metadata = normalize_rows(content, "street_trees", date(2025, 1, 4))

    assert frame.loc[
        0,
        [
            "tree_id",
            "district",
            "location",
            "location_note",
            "species",
            "diameter_cm",
            "height_m",
            "survey_date",
            "updated_at",
        ],
    ].tolist() == [
        "T-100",
        "大安區",
        "仁愛路",
        "靠近入口",
        "榕樹",
        24.5,
        8.2,
        "2025-01-02",
        "2025-01-03",
    ]
    assert metadata.canonical_headers == [
        "tree_id",
        "district",
        "location",
        "location_note",
        "species",
        "diameter_cm",
        "height_m",
        "survey_date",
        "updated_at",
    ]


@pytest.mark.parametrize("content", [b"TreeID\n \n", b"TreeID\nA-1\nA-1\n"])
def test_normalize_rejects_empty_or_duplicate_tree_ids(content: bytes) -> None:
    with pytest.raises(ValueError, match="tree_id"):
        normalize_rows(content, "street_trees", date(2025, 1, 4))


def test_normalize_all_writes_snapshot_schema_and_latest_file(tmp_path: Path) -> None:
    raw = tmp_path / "raw"
    out = tmp_path / "processed"
    content = b"TreeID,Dist\nB-2,Da-an\nA-1,Zhongshan\n"
    _write_raw(raw, "street_trees", "2025-01-04", content)
    _write_raw(raw, "protected_trees", "2025-01-04", b"TreeID\nP-1\n")
    _write_raw(
        raw,
        "park_trees",
        "2025-01-04",
        "TreeID,Dist,ParkName\nPK-1,大安區,大安森林公園\n".encode("utf-8"),
    )

    snapshots = normalize_all(raw, out)

    assert [(item.source, item.snapshot_date.isoformat()) for item in snapshots] == [
        ("park_trees", "2025-01-04"),
        ("protected_trees", "2025-01-04"),
        ("street_trees", "2025-01-04"),
    ]
    snapshot = out / "snapshots" / "street_trees" / "2025-01-04.parquet"
    assert pd.read_parquet(snapshot)["tree_id"].tolist() == ["A-1", "B-2"]
    schema = json.loads(snapshot.with_suffix(".schema.json").read_text(encoding="utf-8"))
    assert schema == {
        "canonical_headers": ["tree_id", "district"],
        "encoding": "utf-8-sig",
        "original_headers": ["TreeID", "Dist"],
        "row_count": 2,
        "sha256": hashlib.sha256(content).hexdigest(),
    }
    assert (out / "trees.parquet").exists()
    assert (out / "protected_trees.parquet").exists()
    assert (out / "park_trees.parquet").exists()


@pytest.mark.parametrize("filename", ["20250731", "2025-W31-4", "2025-7-31"])
def test_normalize_all_rejects_non_exact_snapshot_date_filenames(
    tmp_path: Path, filename: str
) -> None:
    _write_raw(tmp_path / "raw", "street_trees", filename, b"TreeID\nT-1\n")

    with pytest.raises(ValueError, match="YYYY-MM-DD"):
        normalize_all(tmp_path / "raw", tmp_path / "processed")
