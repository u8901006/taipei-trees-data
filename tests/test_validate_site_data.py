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
        path.write_text(json.dumps([{"id": str(item)} for item in range(count)]), encoding="utf-8")
        districts.append({"name": f"第{index}區", "count": count, "file": relative})
    (root / "manifest.json").write_text(
        json.dumps(
            {
                "total_count": sum(counts),
                "district_count": len(counts),
                "districts": districts,
            }
        ),
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
