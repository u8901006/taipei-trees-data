from __future__ import annotations

import gzip
import hashlib
import json
from contextlib import contextmanager
from datetime import date, datetime
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
        self.calls: list[tuple[str, str, dict[str, object]]] = []

    @contextmanager
    def stream(self, method: str, url: str, **kwargs: object):
        self.calls.append((method, url, kwargs))
        yield self.routes[url]

    def get(self, url: str, **kwargs: object) -> httpx.Response:
        self.calls.append(("GET", url, kwargs))
        return self.routes[url]


def csv_response(content: bytes) -> httpx.Response:
    return httpx.Response(
        200,
        content=content,
        headers={"content-type": "text/csv", "content-length": str(len(content))},
    )


def redirect_response(location: str) -> httpx.Response:
    return httpx.Response(302, headers={"location": location})


def test_fetch_writes_deterministic_gzip_that_round_trips_fixture(tmp_path: Path) -> None:
    source = SourceConfig("street_trees", "https://data.taipei/street.csv", None, True)

    result = fetch_dataset(
        source, tmp_path, date(2026, 7, 31), FakeClient({source.url: csv_response(FIXTURE_BYTES)})
    )

    assert result.changed is True
    assert result.checksum == hashlib.sha256(FIXTURE_BYTES).hexdigest()
    assert gzip.decompress(result.path.read_bytes()) == FIXTURE_BYTES
    assert result.path.read_bytes()[4:8] == b"\x00\x00\x00\x00"
    manifest = json.loads(
        result.path.with_suffix("").with_suffix(".json").read_text(encoding="utf-8")
    )
    assert manifest["source_name"] == "street_trees"
    assert manifest["uncompressed_byte_length"] == len(FIXTURE_BYTES)


def test_repeat_fetch_of_identical_bytes_is_unchanged(tmp_path: Path) -> None:
    source = SourceConfig("street_trees", "https://data.taipei/street.csv", None, True)
    client = FakeClient({source.url: csv_response(FIXTURE_BYTES)})

    first = fetch_dataset(source, tmp_path, date(2026, 7, 31), client)
    second = fetch_dataset(source, tmp_path, date(2026, 7, 31), client)

    assert first.changed is True
    assert second.changed is False
    assert second.status == "unchanged"


@pytest.mark.parametrize(
    ("clock", "error"),
    [
        (lambda: datetime(2026, 7, 31), ValueError),
        (
            lambda: (_ for _ in ()).throw(RuntimeError("clock unavailable")),
            RuntimeError,
        ),
    ],
)
def test_invalid_or_failing_clock_cannot_leave_raw_artifacts(
    tmp_path: Path,
    clock: object,
    error: type[Exception],
) -> None:
    source = SourceConfig("street_trees", "https://data.taipei/street.csv", None, True)
    assert callable(clock)

    with pytest.raises(error):
        fetch_dataset(
            source,
            tmp_path,
            date(2026, 7, 31),
            FakeClient({source.url: csv_response(FIXTURE_BYTES)}),
            clock=clock,
        )

    assert not list(tmp_path.rglob("*"))


def test_unchanged_snapshot_recovers_a_missing_manifest(tmp_path: Path) -> None:
    source = SourceConfig("street_trees", "https://data.taipei/street.csv", None, True)
    client = FakeClient({source.url: csv_response(FIXTURE_BYTES)})
    first = fetch_dataset(source, tmp_path, date(2026, 7, 31), client)
    manifest_path = first.path.with_suffix("").with_suffix(".json")
    manifest_path.unlink()

    second = fetch_dataset(source, tmp_path, date(2026, 7, 31), client)

    assert second.status == "unchanged"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["sha256"] == hashlib.sha256(FIXTURE_BYTES).hexdigest()
    assert manifest["source_name"] == source.name


def test_unchanged_snapshot_rejects_an_inconsistent_manifest(tmp_path: Path) -> None:
    source = SourceConfig("street_trees", "https://data.taipei/street.csv", None, True)
    client = FakeClient({source.url: csv_response(FIXTURE_BYTES)})
    first = fetch_dataset(source, tmp_path, date(2026, 7, 31), client)
    manifest_path = first.path.with_suffix("").with_suffix(".json")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["sha256"] = "not-the-snapshot-checksum"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(ImmutableSnapshotError, match="manifest"):
        fetch_dataset(source, tmp_path, date(2026, 7, 31), client)


@pytest.mark.parametrize(
    "url, secret",
    [
        ("https://person:password-value@data.taipei/street.csv", "password-value"),
        ("https://data.taipei/street.csv?Access_Token=token-value", "token-value"),
        (
            "https://data.taipei/street.csv?client_secret=client-secret-value",
            "client-secret-value",
        ),
        ("https://data.taipei/street.csv?X-Amz-Signature=signature-value", "signature-value"),
        (
            "https://data.taipei/street.csv?authorization=authorization-value",
            "authorization-value",
        ),
        ("https://data.taipei/street.csv?bearer_token=bearer-value", "bearer-value"),
        ("https://data.taipei/street.csv#access_token=fragment-secret", "fragment-secret"),
    ],
)
def test_sensitive_source_urls_are_rejected_without_echoing_secrets(
    tmp_path: Path, url: str, secret: str
) -> None:
    source = SourceConfig("street_trees", url, None, True)

    with pytest.raises(ValueError) as error:
        fetch_dataset(source, tmp_path, date(2026, 7, 31), FakeClient({}))

    assert secret not in str(error.value)


def test_non_sensitive_csv_format_query_is_allowed(tmp_path: Path) -> None:
    url = "https://data.taipei/street.csv?format=csv"
    source = SourceConfig("street_trees", url, None, True)

    result = fetch_dataset(
        source, tmp_path, date(2026, 7, 31), FakeClient({url: csv_response(FIXTURE_BYTES)})
    )

    assert result.status == "created"


@pytest.mark.parametrize(
    "url",
    [
        "http://data.taipei/street.csv",
        "https://evil.example/street.csv",
        "https://127.0.0.1/street.csv",
        "https://data.taipei.evil.example/street.csv",
    ],
)
def test_non_official_or_non_https_download_url_is_rejected_before_request(
    tmp_path: Path, url: str
) -> None:
    source = SourceConfig("street_trees", url, None, True)
    client = FakeClient({})

    with pytest.raises(ValueError, match="official HTTPS"):
        fetch_dataset(source, tmp_path, date(2026, 7, 31), client)

    assert client.calls == []
    assert not list(tmp_path.rglob("*"))


def test_download_follows_only_validated_same_host_redirects(tmp_path: Path) -> None:
    start = "https://data.taipei/start.csv"
    final = "https://data.taipei/files/street.csv"
    client = FakeClient(
        {
            start: redirect_response("/files/street.csv"),
            final: csv_response(FIXTURE_BYTES),
        }
    )

    result = fetch_dataset(
        SourceConfig("street_trees", start, None, True),
        tmp_path,
        date(2026, 7, 31),
        client,
    )

    assert result.status == "created"
    assert [call[1] for call in client.calls] == [start, final]
    assert all(call[2]["follow_redirects"] is False for call in client.calls)


def test_download_rejects_cross_host_redirect_before_second_request(tmp_path: Path) -> None:
    start = "https://data.taipei/start.csv"
    client = FakeClient({start: redirect_response("https://evil.example/private.csv")})

    with pytest.raises(ValueError, match="official HTTPS"):
        fetch_dataset(
            SourceConfig("street_trees", start, None, True),
            tmp_path,
            date(2026, 7, 31),
            client,
        )

    assert [call[1] for call in client.calls] == [start]
    assert not list(tmp_path.rglob("*"))


def test_metadata_follows_only_validated_same_host_redirects() -> None:
    start = "https://data.taipei/api/v1/dataset/test-id"
    final = "https://data.taipei/api/v1/dataset/final"
    payload = {
        "result": {
            "resources": [
                {
                    "id": "street",
                    "name": "行道樹資料",
                    "format": "CSV",
                    "url": "https://data.taipei/street.csv",
                }
            ]
        }
    }
    client = FakeClient(
        {
            start: redirect_response("/api/v1/dataset/final"),
            final: httpx.Response(200, json=payload),
        }
    )

    resources = resolve_dataset_resources("test-id", client)

    assert [resource.identifier for resource in resources] == ["street"]
    assert [call[1] for call in client.calls] == [start, final]
    assert all(call[2]["follow_redirects"] is False for call in client.calls)


def test_metadata_rejects_cross_host_redirect_before_second_request() -> None:
    start = "https://data.taipei/api/v1/dataset/test-id"
    client = FakeClient({start: redirect_response("https://evil.example/metadata")})

    with pytest.raises(ValueError, match="official HTTPS"):
        resolve_dataset_resources("test-id", client)

    assert [call[1] for call in client.calls] == [start]


def test_existing_gzip_with_same_uncompressed_bytes_is_unchanged(tmp_path: Path) -> None:
    source = SourceConfig("street_trees", "https://data.taipei/street.csv", None, True)
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
    source = SourceConfig("street_trees", "https://data.taipei/street.csv", None, True)
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


def test_street_tree_resource_selection_ignores_park_and_tree_hole_resources(
    tmp_path: Path,
) -> None:
    dataset_url = "https://data.taipei/api/v1/dataset/test-id"
    metadata = {
        "result": {
            "resources": [
                {
                    "id": "park",
                    "name": "公園樹木",
                    "format": "CSV",
                    "url": "https://data.taipei/park.csv",
                },
                {
                    "id": "hole",
                    "name": "樹穴資料",
                    "format": "CSV",
                    "url": "https://data.taipei/hole.csv",
                },
                {
                    "id": "street",
                    "name": "行道樹資料",
                    "format": "CSV",
                    "url": "https://data.taipei/street.csv",
                },
            ]
        }
    }

    client = FakeClient(
        {
            dataset_url: httpx.Response(200, json=metadata),
            "https://data.taipei/street.csv": csv_response(FIXTURE_BYTES),
        }
    )
    source = SourceConfig("street_trees", None, "test-id", True)

    resources = resolve_dataset_resources("test-id", client)
    result = fetch_dataset(source, tmp_path, date(2026, 7, 31), client)

    assert [resource.identifier for resource in resources] == ["park", "hole", "street"]
    assert result.path.name == "2026-07-31.csv.gz"


def test_protected_tree_dataset_uses_its_csv_resource(tmp_path: Path) -> None:
    dataset_url = "https://data.taipei/api/v1/dataset/protected-id"
    protected_url = "https://data.taipei/protected.csv"
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


def test_optional_source_without_url_or_dataset_id_is_explicitly_skipped(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    config = tmp_path / "sources.json"
    config.write_text(json.dumps({"protected_trees": {"required": False}}), encoding="utf-8")

    assert main(["--out", str(tmp_path / "raw"), "--config", str(config)]) == 0

    assert "protected_trees" in capsys.readouterr().out


def test_required_unavailable_source_fails(tmp_path: Path) -> None:
    source = SourceConfig("street_trees", None, None, True)

    with pytest.raises(RuntimeError, match="required"):
        fetch_dataset(source, tmp_path, date(2026, 7, 31), FakeClient({}))


def test_html_masquerading_as_csv_is_rejected(tmp_path: Path) -> None:
    source = SourceConfig("street_trees", "https://data.taipei/street.csv", None, True)
    client = FakeClient(
        {source.url: csv_response(b"<!doctype html><html><body>Error</body></html>")}
    )

    with pytest.raises(ValueError, match="HTML"):
        fetch_dataset(source, tmp_path, date(2026, 7, 31), client)


def test_github_output_file_has_exact_required_keys(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = tmp_path / "sources.json"
    config.write_text(json.dumps({"protected_trees": {"required": False}}), encoding="utf-8")
    output = tmp_path / "github-output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))

    assert main(["--out", str(tmp_path / "raw"), "--config", str(config)]) == 0

    assert output.read_text(encoding="utf-8").splitlines() == [
        "changed=false",
        "fetched_count=0",
        "skipped_sources=protected_trees",
    ]


def test_main_ignores_non_raw_snapshot_sources(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    config = tmp_path / "sources.json"
    config.write_text(
        json.dumps({"review_records": {"url": "https://example.test/page", "required": False}}),
        encoding="utf-8",
    )

    assert main(["--out", str(tmp_path / "raw"), "--config", str(config)]) == 0

    assert capsys.readouterr().out == ""
