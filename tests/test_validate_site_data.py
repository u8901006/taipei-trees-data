from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.validate_site_data import validate_site_data


def write_index(root: Path, counts: list[int]) -> None:
    districts = []
    for index, count in enumerate(counts):
        relative = f"districts/{index}.json"
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps([{"id": f"{index}-{item}"} for item in range(count)]),
            encoding="utf-8",
        )
        districts.append({"name": f"第{index}區", "count": count, "file": relative})
    (root / "manifest.json").write_text(
        json.dumps(
            {
                "schema_version": 3,
                "total_count": sum(counts),
                "district_count": len(counts),
                "type_counts": {"park": 0, "protected": 0, "street": sum(counts)},
                "schedule_count": 0,
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
                "districts": districts,
            }
        ),
        encoding="utf-8",
    )
    (root / "schedules.json").write_text(
        json.dumps({"schema_version": 1, "retrieved_at": None, "schedules": []}),
        encoding="utf-8",
    )
    (root / "schedule_matches.json").write_text(
        json.dumps({"schema_version": 1, "matches": []}), encoding="utf-8"
    )
    (root / "species_profiles.json").write_text(
        json.dumps({"schema_version": 1, "updated_at": None, "profiles": []}),
        encoding="utf-8",
    )


def test_validate_site_data_accepts_consistent_nonempty_index(tmp_path: Path) -> None:
    write_index(tmp_path, [2, 1])

    validate_site_data(tmp_path, minimum_total=3, expected_districts=2)


@pytest.mark.parametrize("counts", [[], [0, 0], [2, 0]])
def test_validate_site_data_rejects_empty_or_truncated_index(
    tmp_path: Path, counts: list[int]
) -> None:
    write_index(tmp_path, counts)

    with pytest.raises(ValueError):
        validate_site_data(tmp_path, minimum_total=3, expected_districts=2)


def test_validate_site_data_rejects_partition_count_mismatch(tmp_path: Path) -> None:
    write_index(tmp_path, [2, 1])
    (tmp_path / "districts" / "0.json").write_text("[]", encoding="utf-8")

    with pytest.raises(ValueError, match="count"):
        validate_site_data(tmp_path, minimum_total=3, expected_districts=2)


def test_validate_site_data_rejects_unknown_schedule_reference(tmp_path: Path) -> None:
    write_index(tmp_path, [1])
    (tmp_path / "schedule_matches.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "matches": [
                    {
                        "schedule_id": "missing",
                        "tree_id": "0",
                        "match_method": "street_location_phrase",
                        "explanation": "candidate",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="schedule"):
        validate_site_data(tmp_path, minimum_total=1, expected_districts=1)


def test_validate_site_data_enforces_minimum_protected_count(tmp_path: Path) -> None:
    write_index(tmp_path, [3])

    with pytest.raises(ValueError, match="protected"):
        validate_site_data(
            tmp_path, minimum_total=3, expected_districts=1, minimum_protected=1
        )
