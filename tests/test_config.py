"""來源設定的公開行為測試。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.config import SourceConfig, load_sources


def write_sources(tmp_path: Path, sources: dict[str, object]) -> Path:
    """寫入最小來源設定檔，供每個測試隔離使用。"""
    path = tmp_path / "sources.json"
    path.write_text(json.dumps(sources), encoding="utf-8")
    return path


def test_environment_overrides_street_tree_dataset_id(tmp_path: Path) -> None:
    path = write_sources(
        tmp_path,
        {"street_trees": {"dataset_id": "default", "required": True}},
    )

    sources = load_sources(path, {"TAIPEI_STREET_TREES_ID": "override"})

    assert sources["street_trees"].dataset_id == "override"


def test_repository_config_declares_required_official_park_tree_csv() -> None:
    config = Path(__file__).parents[1] / "config" / "sources.json"

    sources = load_sources(config, {})

    assert sources["park_trees"].url == (
        "https://tppkl.blob.core.windows.net/blobfs/TaipeiParkTree.csv"
    )
    assert sources["park_trees"].required is True


def test_repository_config_declares_required_official_protected_tree_csv() -> None:
    config = Path(__file__).parents[1] / "config" / "sources.json"

    sources = load_sources(config, {})

    assert sources["protected_trees"].dataset_id == "d9d1140b-c2c3-405f-8dd1-7b87b00f6f53"
    assert sources["protected_trees"].url == (
        "https://data.taipei/api/frontstage/tpeod/dataset/resource.download"
        "?rid=a7c2db0d-8b6e-42b2-bdcc-ae69a6797e1d"
    )
    assert sources["protected_trees"].required is True


@pytest.mark.parametrize(
    ("source_name", "environment_name", "field_name"),
    [
        ("protected_trees", "TAIPEI_PROTECTED_TREES_ID", "dataset_id"),
        ("pruning_schedule", "TAIPEI_PRUNING_SCHEDULE_URL", "url"),
        ("review_records", "TAIPEI_REVIEW_RECORDS_URL", "url"),
        ("committee_records", "TAIPEI_COMMITTEE_RECORDS_URL", "url"),
    ],
)
def test_environment_mapping_exists_for_each_optional_source(
    tmp_path: Path,
    source_name: str,
    environment_name: str,
    field_name: str,
) -> None:
    path = write_sources(tmp_path, {source_name: {"required": False}})

    sources = load_sources(path, {environment_name: "https://example.test/source"})

    source = sources[source_name]
    assert getattr(source, field_name) == "https://example.test/source"
    assert source.available is True


def test_optional_missing_source_is_explicit_and_unavailable(tmp_path: Path) -> None:
    path = write_sources(tmp_path, {"protected_trees": {"url": None, "required": False}})

    sources = load_sources(path, {})

    assert sources["protected_trees"].available is False


def test_source_config_is_immutable() -> None:
    source = SourceConfig(name="protected_trees", url=None, dataset_id=None, required=False)

    with pytest.raises(AttributeError):
        source.name = "renamed"  # type: ignore[misc]
