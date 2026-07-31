from __future__ import annotations

import gzip
import hashlib
import json
from contextlib import contextmanager
from datetime import date
from pathlib import Path

import httpx
import pytest

from scripts.config import SourceConfig
from scripts.fetch_opendata import fetch_dataset, main
from scripts.io_utils import ImmutableSnapshotError
from scripts.taipei_api import resolve_dataset_resources


FIXTURE_BYTES = (Path(__file__).parent / "fixtures" / "street_trees.csv").read_bytes()


class FakeClient:
    def __init__(self, routes: dict[str, httpx.Response]) -> None:
        self.routes = routes

    @contextmanager
    def stream(self, method: str, url: str, **kwargs: object):
        yield self.routes[url]

    def get(self, url: str, **kwargs: object) -> httpx.Response:
        return self.routes[url]


def csv_response(content: bytes) -> httpx.Response:
    return httpx.Response(
        200,
        content=content,
        headers={"content-type": "text/csv", "content-length": str(len(content))},
    )


def test_fetch_writes_deterministic_gzip_that_round_trips_fixture(tmp_path: Path) -> None:
    source = SourceConfig("street_trees", "https://example.test/street.csv", None, True)

    result = fetch_dataset(source, tmp_path, date(2026, 7, 31), FakeClient({source.url: csv_response(FIXTURE_BYTES)}))

    assert result.changed is True
    assert result.checksum == hashlib.sha256(FIXTURE_BYTES).hexdigest()
    assert gzip.decompress(result.path.read_bytes()) == FIXTURE_BYTES
    assert result.path.read_bytes()[4:8] == b"\x00\x00\x00\x00"
    manifest = json.loads(result.path.with_suffix("").with_suffix(".json").read_text(encoding="utf-8"))
    assert manifest["source_name"] == "street_trees"
    assert manifest["uncompressed_byte_length"] == len(FIXTURE_BYTES)


def test_repeat_fetch_of_identical_bytes_is_unchanged(tmp_path: Path) -> None:
    source = SourceConfig("street_trees", "https://example.test/street.csv", None, True)
    client = FakeClient({source.url: csv_response(FIXTURE_BYTES)})

    first = fetch_dataset(source, tmp_path, date(2026, 7, 31), client)
    second = fetch_dataset(source, tmp_path, date(2026, 7, 31), client)

    assert first.changed is True
    assert second.changed is False
    assert second.status == "unchanged"


def test_existing_gzip_with_same_uncompressed_bytes_is_unchanged(tmp_path: Path) -> None:
    source = SourceConfig("street_trees", "https://example.test/street.csv", None, True)
    snapshot = tmp_path / "street_trees" / "2026-07-31.csv.gz"
    snapshot.parent.mkdir()
    snapshot.write_bytes(gzip.compress(FIXTURE_BYTES, mtime=1))

    result = fetch_dataset(
        source,
        tmp_path,
        date(2026, 7, 31),
        FakeClient({source.url: csv_response(FIXTURE_BYTES)}),
    )

    assert result.status == "unchanged"
    assert gzip.decompress(snapshot.read_bytes()) == FIXTURE_BYTES


def test_different_bytes_for_existing_date_raise_and_leave_snapshot_intact(tmp_path: Path) -> None:
    source = SourceConfig("street_trees", "https://example.test/street.csv", None, True)
    first_client = FakeClient({source.url: csv_response(FIXTURE_BYTES)})
    path = fetch_dataset(source, tmp_path, date(2026, 7, 31), first_client).path
    original = path.read_bytes()
    changed_client = FakeClient(
        {source.url: csv_response("tree_id,name\nT-099,不同\n".encode("utf-8"))}
    )

    with pytest.raises(ImmutableSnapshotError):
        fetch_dataset(source, tmp_path, date(2026, 7, 31), changed_client)

    assert path.read_bytes() == original
    assert gzip.decompress(path.read_bytes()) == FIXTURE_BYTES


def test_street_tree_resource_selection_ignores_park_and_tree_hole_resources(tmp_path: Path) -> None:
    dataset_url = "https://data.taipei/api/v1/dataset/test-id"
    metadata = {
        "result": {
            "resources": [
                {"id": "park", "name": "公園樹木", "format": "CSV", "url": "https://example.test/park.csv"},
                {"id": "hole", "name": "樹穴資料", "format": "CSV", "url": "https://example.test/hole.csv"},
                {"id": "street", "name": "行道樹資料", "format": "CSV", "url": "https://example.test/street.csv"},
            ]
        }
    }

    client = FakeClient(
        {
            dataset_url: httpx.Response(200, json=metadata),
            "https://example.test/street.csv": csv_response(FIXTURE_BYTES),
        }
    )
    source = SourceConfig("street_trees", None, "test-id", True)

    resources = resolve_dataset_resources("test-id", client)
    result = fetch_dataset(source, tmp_path, date(2026, 7, 31), client)

    assert [resource.identifier for resource in resources] == ["park", "hole", "street"]
    assert result.path.name == "2026-07-31.csv.gz"


def test_protected_tree_dataset_uses_its_csv_resource(tmp_path: Path) -> None:
    dataset_url = "https://data.taipei/api/v1/dataset/protected-id"
    protected_url = "https://example.test/protected.csv"
    client = FakeClient(
        {
            dataset_url: httpx.Response(
                200,
                json={
                    "result": {
                        "resources": [
                            {
                                "id": "protected-resource",
                                "name": "受保護樹木資料",
                                "format": "CSV",
                                "url": protected_url,
                            }
                        ]
                    }
                },
            ),
            protected_url: csv_response(FIXTURE_BYTES),
        }
    )
    source = SourceConfig("protected_trees", None, "protected-id", False)

    result = fetch_dataset(source, tmp_path, date(2026, 7, 31), client)

    assert result.path == tmp_path / "protected_trees" / "2026-07-31.csv.gz"


def test_optional_source_without_url_or_dataset_id_is_explicitly_skipped(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    config = tmp_path / "sources.json"
    config.write_text(json.dumps({"protected_trees": {"required": False}}), encoding="utf-8")

    assert main(["--out", str(tmp_path / "raw"), "--config", str(config)]) == 0

    assert "protected_trees" in capsys.readouterr().out


def test_required_unavailable_source_fails(tmp_path: Path) -> None:
    source = SourceConfig("street_trees", None, None, True)

    with pytest.raises(RuntimeError, match="required"):
        fetch_dataset(source, tmp_path, date(2026, 7, 31), FakeClient({}))


def test_html_masquerading_as_csv_is_rejected(tmp_path: Path) -> None:
    source = SourceConfig("street_trees", "https://example.test/street.csv", None, True)
    client = FakeClient({source.url: csv_response(b"<!doctype html><html><body>Error</body></html>")})

    with pytest.raises(ValueError, match="HTML"):
        fetch_dataset(source, tmp_path, date(2026, 7, 31), client)


def test_github_output_file_has_exact_required_keys(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = tmp_path / "sources.json"
    config.write_text(
        json.dumps({"protected_trees": {"required": False}}), encoding="utf-8"
    )
    output = tmp_path / "github-output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))

    assert main(["--out", str(tmp_path / "raw"), "--config", str(config)]) == 0

    assert output.read_text(encoding="utf-8").splitlines() == [
        "changed=false",
        "fetched_count=0",
        "skipped_sources=protected_trees",
    ]


def test_main_ignores_non_raw_snapshot_sources(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    config = tmp_path / "sources.json"
    config.write_text(
        json.dumps({"review_records": {"url": "https://example.test/page", "required": False}}),
        encoding="utf-8",
    )

    assert main(["--out", str(tmp_path / "raw"), "--config", str(config)]) == 0

    assert capsys.readouterr().out == ""
