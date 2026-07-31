"""Pruning-schedule fetch security and immutability contracts."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Callable

import httpx
import pytest

import scripts.fetch_schedule as schedule
from scripts.fetch_schedule import ScheduleFetchError, fetch_schedule, main
from scripts.io_utils import ImmutableSnapshotError


NOW = datetime(2026, 7, 31, 16, 5, 6, tzinfo=UTC)  # 2026-08-01 in Taipei.
OFFICIAL_URL = "https://data.taipei/pruning/schedule"


class FakeResponse:
    def __init__(
        self,
        status_code: int = 200,
        content: bytes = b"district,date\nDatong,2026-08-01\n",
        *,
        content_type: str = "text/csv; charset=utf-8",
        location: str | None = None,
        content_length: str | None = None,
    ) -> None:
        self.status_code = status_code
        self._content = content
        self.headers: dict[str, str] = {"content-type": content_type}
        if location is not None:
            self.headers["location"] = location
        if content_length is not None:
            self.headers["content-length"] = content_length

    def __enter__(self) -> FakeResponse:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def iter_bytes(self) -> list[bytes]:
        midpoint = max(1, len(self._content) // 2)
        return [self._content[:midpoint], self._content[midpoint:]]


class FakeClient:
    def __init__(self, outcomes: list[FakeResponse | Exception]) -> None:
        self.outcomes = list(outcomes)
        self.calls: list[tuple[str, dict[str, object]]] = []

    def stream(self, method: str, url: str, **kwargs: object) -> FakeResponse:
        assert method == "GET"
        self.calls.append((url, kwargs))
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    def __enter__(self) -> FakeClient:
        return self

    def __exit__(self, *_args: object) -> None:
        return None


def write_config(path: Path, url: str | None) -> None:
    path.write_text(
        json.dumps({"pruning_schedule": {"url": url, "required": False}}),
        encoding="utf-8",
    )


def manifest_for(snapshot: Path) -> Path:
    return snapshot.with_name(f"{snapshot.name}.manifest.json")


def test_no_config_is_successful_zero_network_and_exact_github_output(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config = tmp_path / "sources.json"
    github_output = tmp_path / "github-output"
    write_config(config, None)
    constructed = False

    def forbidden_client_factory() -> FakeClient:
        nonlocal constructed
        constructed = True
        raise AssertionError("network client must not be created")

    status = main(
        ["--out", str(tmp_path / "out"), "--config", str(config)],
        environ={"GITHUB_OUTPUT": str(github_output)},
        client_factory=forbidden_client_factory,
        clock=lambda: NOW,
    )

    assert status == 0
    assert constructed is False
    assert capsys.readouterr().out == "修剪時程來源尚未設定。\n"
    assert github_output.read_text(encoding="utf-8") == (
        "status=not_configured\nnew_files=0\n"
    )


@pytest.mark.parametrize(
    "unsafe_url",
    [
        "http://data.taipei/schedule",
        "https://example.invalid/schedule",
        "https://user:pass@data.taipei/schedule",
        "https://data.taipei/schedule#fragment",
        "https://data.taipei/schedule?api_key=hidden",
    ],
)
def test_initial_url_is_validated_before_network(
    tmp_path: Path,
    unsafe_url: str,
) -> None:
    client = FakeClient([])

    with pytest.raises(ScheduleFetchError, match="schedule fetch failed"):
        fetch_schedule(unsafe_url, tmp_path, client, clock=lambda: NOW)

    assert client.calls == []


def test_safe_relative_redirect_is_followed_without_automatic_redirects(tmp_path: Path) -> None:
    client = FakeClient(
        [
            FakeResponse(302, location="/pruning/final"),
            FakeResponse(content=b"district,date\nXinyi,2026-08-01\n"),
        ]
    )

    result = fetch_schedule(OFFICIAL_URL, tmp_path, client, clock=lambda: NOW)

    assert result.status == "created"
    assert [call[0] for call in client.calls] == [
        OFFICIAL_URL,
        "https://data.taipei/pruning/final",
    ]
    assert all(call[1] == {"timeout": 30.0, "follow_redirects": False} for call in client.calls)


@pytest.mark.parametrize(
    "location",
    [
        "https://example.invalid/file.csv",
        "http://data.taipei/file.csv",
        "https://data.taipei/file.csv?token=hidden",
        "//user:pass@data.taipei/file.csv",
    ],
)
def test_every_redirect_is_validated_before_request(tmp_path: Path, location: str) -> None:
    client = FakeClient([FakeResponse(302, location=location)])

    with pytest.raises(ScheduleFetchError, match="schedule fetch failed"):
        fetch_schedule(OFFICIAL_URL, tmp_path, client, clock=lambda: NOW)

    assert len(client.calls) == 1


def test_redirect_cycle_and_hop_count_are_bounded(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cycle = FakeClient(
        [
            FakeResponse(302, location="/second"),
            FakeResponse(302, location="/pruning/schedule"),
        ]
    )
    with pytest.raises(ScheduleFetchError):
        fetch_schedule(OFFICIAL_URL, tmp_path, cycle, clock=lambda: NOW)
    assert len(cycle.calls) == 2

    monkeypatch.setattr(schedule, "_MAX_REDIRECT_HOPS", 1)
    hops = FakeClient(
        [
            FakeResponse(302, location="/one"),
            FakeResponse(302, location="/two"),
        ]
    )
    with pytest.raises(ScheduleFetchError):
        fetch_schedule(OFFICIAL_URL, tmp_path, hops, clock=lambda: NOW)
    assert len(hops.calls) == 2


@pytest.mark.parametrize("status_code", [408, 429, 500, 501, 509, 599])
def test_every_transient_http_status_is_retried(
    tmp_path: Path,
    status_code: int,
) -> None:
    client = FakeClient([FakeResponse(status_code), FakeResponse(200)])

    result = fetch_schedule(
        OFFICIAL_URL,
        tmp_path,
        client,
        clock=lambda: NOW,
        sleeper=lambda _delay: None,
    )

    assert result.status == "created"
    assert len(client.calls) == 2


def test_transport_retry_is_bounded(tmp_path: Path) -> None:
    request = httpx.Request("GET", OFFICIAL_URL)
    client = FakeClient([httpx.ReadTimeout("hidden", request=request)] * 3)

    with pytest.raises(ScheduleFetchError, match="schedule fetch failed"):
        fetch_schedule(
            OFFICIAL_URL,
            tmp_path,
            client,
            clock=lambda: NOW,
            sleeper=lambda _delay: None,
        )

    assert len(client.calls) == 3


@pytest.mark.parametrize("declared_length", ["9", "not-a-number", "-1"])
def test_declared_response_size_is_strict(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    declared_length: str,
) -> None:
    monkeypatch.setattr(schedule, "_MAX_DOWNLOAD_BYTES", 8)
    client = FakeClient(
        [FakeResponse(content=b"small", content_length=declared_length)]
    )

    with pytest.raises(ScheduleFetchError):
        fetch_schedule(OFFICIAL_URL, tmp_path, client, clock=lambda: NOW)


def test_streamed_response_size_is_bounded(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(schedule, "_MAX_DOWNLOAD_BYTES", 8)
    client = FakeClient([FakeResponse(content=b"123456789")])

    with pytest.raises(ScheduleFetchError):
        fetch_schedule(OFFICIAL_URL, tmp_path, client, clock=lambda: NOW)


@pytest.mark.parametrize(
    ("content_type", "content"),
    [
        ("text/html", b"<html>not a schedule</html>"),
        ("application/octet-stream", b"district,date\n"),
        ("application/pdf", b"not-pdf"),
        ("application/json", b"{not-json}"),
        ("text/plain", b"\x00\x00\x00"),
        ("text/csv", b"<!doctype html><title>error</title>"),
    ],
)
def test_content_type_and_magic_or_text_sanity_are_required(
    tmp_path: Path,
    content_type: str,
    content: bytes,
) -> None:
    client = FakeClient([FakeResponse(content=content, content_type=content_type)])

    with pytest.raises(ScheduleFetchError):
        fetch_schedule(OFFICIAL_URL, tmp_path, client, clock=lambda: NOW)


@pytest.mark.parametrize(
    ("content_type", "content", "extension"),
    [
        ("text/plain", "大安區 8/1".encode(), ".txt"),
        ("text/csv", b"district,date\nXinyi,2026-08-01\n", ".csv"),
        ("application/json", b'{"district":"Xinyi"}', ".json"),
        ("application/pdf", b"%PDF-1.7\nsafe", ".pdf"),
    ],
)
def test_expected_content_types_are_archived_with_taipei_date_and_strict_manifest(
    tmp_path: Path,
    content_type: str,
    content: bytes,
    extension: str,
) -> None:
    result = fetch_schedule(
        OFFICIAL_URL,
        tmp_path,
        FakeClient([FakeResponse(content=content, content_type=content_type)]),
        clock=lambda: NOW,
    )

    assert result.path == tmp_path / "2026-08-01" / f"pruning_schedule{extension}"
    assert result.path.read_bytes() == content
    manifest_path = manifest_for(result.path)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest == {
        "schema_version": 1,
        "source_url": OFFICIAL_URL,
        "sha256": hashlib.sha256(content).hexdigest(),
        "byte_length": len(content),
        "content_type": content_type,
        "retrieved_at": NOW.isoformat(),
    }
    assert manifest_path.read_bytes().endswith(b"\n")


def test_identical_snapshot_is_unchanged_and_conflict_fails_closed(tmp_path: Path) -> None:
    original = b"district,date\nXinyi,2026-08-01\n"
    created = fetch_schedule(
        OFFICIAL_URL,
        tmp_path,
        FakeClient([FakeResponse(content=original)]),
        clock=lambda: NOW,
    )
    unchanged = fetch_schedule(
        OFFICIAL_URL,
        tmp_path,
        FakeClient([FakeResponse(content=original)]),
        clock=lambda: NOW,
    )
    assert created.status == "created"
    assert unchanged.status == "unchanged"

    with pytest.raises(ImmutableSnapshotError):
        fetch_schedule(
            OFFICIAL_URL,
            tmp_path,
            FakeClient([FakeResponse(content=b"different,bytes\n")]),
            clock=lambda: NOW,
        )
    assert created.path.read_bytes() == original


@pytest.mark.parametrize(
    "mutation",
    [
        lambda manifest: manifest.pop("sha256"),
        lambda manifest: manifest.update({"extra": "field"}),
        lambda manifest: manifest.update({"source_url": "https://example.invalid/secret"}),
        lambda manifest: manifest.update({"sha256": "0" * 64}),
        lambda manifest: manifest.update({"byte_length": -1}),
        lambda manifest: manifest.update({"content_type": "text/html"}),
        lambda manifest: manifest.update({"retrieved_at": "2026-07-31T16:05:06"}),
    ],
)
def test_existing_manifest_is_validated_strictly_before_unchanged(
    tmp_path: Path,
    mutation: Callable[[dict[str, object]], object],
) -> None:
    content = b"district,date\nXinyi,2026-08-01\n"
    first = fetch_schedule(
        OFFICIAL_URL,
        tmp_path,
        FakeClient([FakeResponse(content=content)]),
        clock=lambda: NOW,
    )
    manifest_path = manifest_for(first.path)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    mutation(manifest)
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(ImmutableSnapshotError):
        fetch_schedule(
            OFFICIAL_URL,
            tmp_path,
            FakeClient([FakeResponse(content=content)]),
            clock=lambda: NOW,
        )


def test_orphan_snapshot_or_manifest_and_changed_type_fail_closed(tmp_path: Path) -> None:
    day = tmp_path / "2026-08-01"
    day.mkdir()
    orphan = day / "pruning_schedule.csv"
    orphan.write_bytes(b"orphan")
    with pytest.raises(ImmutableSnapshotError):
        fetch_schedule(
            OFFICIAL_URL,
            tmp_path,
            FakeClient([FakeResponse()]),
            clock=lambda: NOW,
        )

    orphan.unlink()
    manifest_for(orphan).write_text("{}", encoding="utf-8")
    with pytest.raises(ImmutableSnapshotError):
        fetch_schedule(
            OFFICIAL_URL,
            tmp_path,
            FakeClient([FakeResponse()]),
            clock=lambda: NOW,
        )


def test_cli_available_outputs_count_and_fixed_safe_errors(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config = tmp_path / "sources.json"
    github_output = tmp_path / "github-output"
    secret_literal = "DO-NOT-PRINT"
    write_config(config, f"{OFFICIAL_URL}?view={secret_literal}")

    status = main(
        ["--out", str(tmp_path / "out"), "--config", str(config)],
        environ={"GITHUB_OUTPUT": str(github_output)},
        client_factory=lambda: FakeClient(
            [FakeResponse(content=b"<html>unsafe</html>", content_type="text/html")]
        ),
        clock=lambda: NOW,
    )

    captured = capsys.readouterr()
    assert status == 1
    assert captured.err == "修剪時程擷取失敗。\n"
    assert captured.out == ""
    assert secret_literal not in captured.err
    assert str(tmp_path) not in captured.err
    assert not github_output.exists()


def test_cli_available_writes_exact_github_output(tmp_path: Path) -> None:
    config = tmp_path / "sources.json"
    github_output = tmp_path / "github-output"
    write_config(config, OFFICIAL_URL)
    environ = {"GITHUB_OUTPUT": str(github_output)}

    first = main(
        ["--out", str(tmp_path / "out"), "--config", str(config)],
        environ=environ,
        client_factory=lambda: FakeClient([FakeResponse()]),
        clock=lambda: NOW,
    )
    second = main(
        ["--out", str(tmp_path / "out"), "--config", str(config)],
        environ=environ,
        client_factory=lambda: FakeClient([FakeResponse()]),
        clock=lambda: NOW,
    )

    assert first == second == 0
    assert github_output.read_text(encoding="utf-8") == (
        "status=available\nnew_files=1\n"
        "status=available\nnew_files=0\n"
    )
