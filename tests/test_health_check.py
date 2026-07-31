from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

import scripts.health_check as health
from scripts.config import SourceConfig
from scripts.health_check import HealthHistoryError, build_health_report, main


class FakeResponse:
    def __init__(
        self,
        status_code: int,
        *,
        content_type: str = "application/json",
        location: str | None = None,
    ) -> None:
        self.status_code = status_code
        self.headers = {"content-type": content_type}
        if location is not None:
            self.headers["location"] = location


class FakeClient:
    def __init__(self, outcomes: list[object]) -> None:
        self.outcomes = outcomes
        self.calls: list[tuple[str, dict[str, object]]] = []

    def get(self, url: str, **kwargs: object) -> FakeResponse:
        self.calls.append((url, kwargs))
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        assert isinstance(outcome, FakeResponse)
        return outcome

    def __enter__(self) -> FakeClient:
        return self

    def __exit__(self, *_args: object) -> None:
        return None


NOW = datetime(2026, 7, 31, 4, 5, 6, tzinfo=UTC)


def source(name: str, *, dataset_id: str | None = None, url: str | None = None) -> SourceConfig:
    return SourceConfig(name=name, dataset_id=dataset_id, url=url, required=False)


def by_name(report: dict[str, object]) -> dict[str, dict[str, object]]:
    return {item["name"]: item for item in report["sources"]}  # type: ignore[index]


def test_successful_dataset_probe_retries_transient_failures_with_bounded_timeout() -> None:
    client = FakeClient([FakeResponse(503), FakeResponse(200)])

    report = build_health_report(
        {"street_trees": source("street_trees", dataset_id="dataset-id")},
        client,
        previous_report=None,
        clock=lambda: NOW,
        sleeper=lambda _delay: None,
    )

    entry = by_name(report)["street_trees"]
    assert entry["status"] == "available"
    assert entry["reason"] is None
    assert entry["unavailable_since"] is None
    assert len(client.calls) == 2
    assert all(call[1]["timeout"] == 15.0 for call in client.calls)
    assert all(call[1]["follow_redirects"] is False for call in client.calls)
    assert all(call[0].endswith("/dataset-id") for call in client.calls)
    assert "dataset-id" not in json.dumps(report)


def test_failure_preserves_first_unavailable_since_and_recovery_clears_it() -> None:
    configured = {"street_trees": source("street_trees", dataset_id="dataset-id")}
    first = build_health_report(
        configured,
        FakeClient([httpx.ConnectError("hidden") for _ in range(3)]),
        previous_report=None,
        clock=lambda: NOW,
        sleeper=lambda _delay: None,
    )
    later = datetime(2026, 8, 1, tzinfo=UTC)
    repeated = build_health_report(
        configured,
        FakeClient([FakeResponse(500) for _ in range(3)]),
        previous_report=first,
        clock=lambda: later,
        sleeper=lambda _delay: None,
    )
    recovered = build_health_report(
        configured,
        FakeClient([FakeResponse(200)]),
        previous_report=repeated,
        clock=lambda: later,
        sleeper=lambda _delay: None,
    )

    assert by_name(first)["street_trees"]["unavailable_since"] == NOW.isoformat()
    assert by_name(repeated)["street_trees"]["unavailable_since"] == NOW.isoformat()
    assert by_name(recovered)["street_trees"]["status"] == "available"
    assert by_name(recovered)["street_trees"]["unavailable_since"] is None


def test_missing_optional_sources_are_explicitly_not_configured_and_stably_sorted() -> None:
    report = build_health_report(
        {
            "review_records": source("review_records"),
            "protected_trees": source("protected_trees"),
        },
        FakeClient([]),
        previous_report=None,
        clock=lambda: NOW,
    )

    assert [entry["name"] for entry in report["sources"]] == ["protected_trees", "review_records"]  # type: ignore[index]
    assert all(entry["status"] == "not_configured" for entry in report["sources"])  # type: ignore[index]
    assert all(entry["reason"] == "source_not_configured" for entry in report["sources"])  # type: ignore[index]


def test_malformed_previous_history_fails_closed_without_echoing_its_contents() -> None:
    with pytest.raises(HealthHistoryError, match="health history is invalid") as caught:
        build_health_report(
            {"street_trees": source("street_trees", dataset_id="dataset-id")},
            FakeClient([]),
            previous_report={"secret": "do-not-echo"},
            clock=lambda: NOW,
        )

    assert "do-not-echo" not in str(caught.value)


def test_dataset_rejects_wrong_content_type_but_url_source_accepts_official_https_page() -> None:
    sources = {
        "dataset": source("dataset", dataset_id="hidden-id"),
        "review": source("review", url="https://culture.gov.taipei/News.aspx?n=public"),
    }
    report = build_health_report(
        sources,
        FakeClient([FakeResponse(200, content_type="text/html"), FakeResponse(200, content_type="text/html")]),
        previous_report=None,
        clock=lambda: NOW,
    )

    entries = by_name(report)
    assert entries["dataset"]["status"] == "unavailable"
    assert entries["review"]["status"] == "available"
    assert "News.aspx" not in json.dumps(report)


def test_unsafe_url_and_redirect_are_never_followed() -> None:
    unsafe = {"review": source("review", url="https://example.invalid/secret?token=hidden")}
    with pytest.raises(Exception, match="source URL is unsafe") as caught:
        build_health_report(unsafe, FakeClient([]), previous_report=None, clock=lambda: NOW)
    assert "hidden" not in str(caught.value)

    client = FakeClient([FakeResponse(302)])
    report = build_health_report(
        {"review": source("review", url="https://culture.gov.taipei/redirect")},
        client,
        previous_report=None,
        clock=lambda: NOW,
    )
    assert by_name(report)["review"]["status"] == "unavailable"
    assert len(client.calls) == 1
    assert client.calls[0][1]["follow_redirects"] is False


@pytest.mark.parametrize(
    "mutation",
    [
        lambda report: report.pop("generated_at"),
        lambda report: report.update({"extra": True}),
        lambda report: report["sources"][0].pop("reason"),
        lambda report: report["sources"][0].update({"unavailable_since": NOW.isoformat()}),
    ],
)
def test_history_requires_exact_schema_and_valid_status_contract(mutation: object) -> None:
    valid = build_health_report(
        {"street_trees": source("street_trees", dataset_id="dataset-id")},
        FakeClient([FakeResponse(200)]),
        previous_report=None,
        clock=lambda: NOW,
    )
    assert callable(mutation)
    mutation(valid)

    with pytest.raises(HealthHistoryError, match="health history is invalid"):
        build_health_report(
            {"street_trees": source("street_trees", dataset_id="dataset-id")},
            FakeClient([]),
            previous_report=valid,
            clock=lambda: NOW,
        )


@pytest.mark.parametrize(
    ("status", "reason", "unavailable_since"),
    [
        ("available", "probe_failed", None),
        ("unavailable", "source_not_configured", NOW.isoformat()),
        ("not_configured", "probe_failed", None),
        ("not_configured", "source_not_configured", NOW.isoformat()),
    ],
)
def test_history_status_reason_and_continuity_contract_is_exact(
    status: str,
    reason: str,
    unavailable_since: str | None,
) -> None:
    history = build_health_report(
        {"street_trees": source("street_trees", dataset_id="dataset-id")},
        FakeClient([FakeResponse(200)]),
        previous_report=None,
        clock=lambda: NOW,
    )
    entry = history["sources"][0]  # type: ignore[index]
    entry.update(
        {
            "status": status,
            "reason": reason,
            "unavailable_since": unavailable_since,
        }
    )

    with pytest.raises(HealthHistoryError, match="health history is invalid"):
        build_health_report(
            {"street_trees": source("street_trees", dataset_id="dataset-id")},
            FakeClient([FakeResponse(200)]),
            previous_report=history,
            clock=lambda: NOW,
        )


def test_missing_known_sources_keep_their_declared_kind() -> None:
    report = build_health_report(
        {
            "street_trees": source("street_trees"),
            "protected_trees": source("protected_trees"),
            "pruning_schedule": source("pruning_schedule"),
            "review_records": source("review_records"),
            "committee_records": source("committee_records"),
        },
        FakeClient([]),
        previous_report=None,
        clock=lambda: NOW,
    )

    assert by_name(report)["street_trees"]["kind"] == "dataset"
    assert by_name(report)["protected_trees"]["kind"] == "dataset"
    assert by_name(report)["pruning_schedule"]["kind"] == "url"
    assert by_name(report)["review_records"]["kind"] == "url"
    assert by_name(report)["committee_records"]["kind"] == "url"


@pytest.mark.parametrize(
    "misconfigured",
    [
        source("protected_trees", url="https://culture.gov.taipei/trees"),
        source("review_records", dataset_id="dataset-id"),
    ],
)
def test_known_source_kind_cannot_be_changed_by_the_wrong_locator(
    misconfigured: SourceConfig,
) -> None:
    with pytest.raises(health.HealthConfigurationError, match="source configuration is invalid"):
        build_health_report(
            {misconfigured.name: misconfigured},
            FakeClient([FakeResponse(200)]),
            previous_report=None,
            clock=lambda: NOW,
        )


def test_continuity_is_not_preserved_when_source_kind_changes() -> None:
    prior_time = "2026-07-01T00:00:00+00:00"
    history = {
        "schema_version": "1.0",
        "generated_at": prior_time,
        "sources": [
            {
                "name": "street_trees",
                "kind": "url",
                "required": False,
                "status": "unavailable",
                "checked_at": prior_time,
                "reason": "probe_failed",
                "unavailable_since": prior_time,
            }
        ],
    }

    report = build_health_report(
        {"street_trees": source("street_trees", dataset_id="dataset-id")},
        FakeClient([FakeResponse(500), FakeResponse(500), FakeResponse(500)]),
        previous_report=history,
        clock=lambda: NOW,
        sleeper=lambda _delay: None,
    )

    assert by_name(report)["street_trees"]["unavailable_since"] == NOW.isoformat()


@pytest.mark.parametrize("server_error", [501, 509, 599])
def test_every_server_error_is_retried(server_error: int) -> None:
    client = FakeClient([FakeResponse(server_error), FakeResponse(200)])

    report = build_health_report(
        {"street_trees": source("street_trees", dataset_id="dataset-id")},
        client,
        previous_report=None,
        clock=lambda: NOW,
        sleeper=lambda _delay: None,
    )

    assert by_name(report)["street_trees"]["status"] == "available"
    assert len(client.calls) == 2


@pytest.mark.parametrize("content_type", ["application/json", "application/json; charset=utf-8", "application/ld+json"])
def test_dataset_accepts_only_exact_json_media_types(content_type: str) -> None:
    report = build_health_report(
        {"street_trees": source("street_trees", dataset_id="dataset-id")},
        FakeClient([FakeResponse(200, content_type=content_type)]),
        previous_report=None,
        clock=lambda: NOW,
    )
    assert by_name(report)["street_trees"]["status"] == "available"


@pytest.mark.parametrize("content_type", ["", "application/octet-stream", "application/notjson", "text/html"])
def test_dataset_rejects_non_json_media_types(content_type: str) -> None:
    report = build_health_report(
        {"street_trees": source("street_trees", dataset_id="dataset-id")},
        FakeClient([FakeResponse(200, content_type=content_type)]),
        previous_report=None,
        clock=lambda: NOW,
    )
    assert by_name(report)["street_trees"]["status"] == "unavailable"


@pytest.mark.parametrize(
    "content_type",
    ["text/html", "text/plain; charset=utf-8", "text/csv", "application/json", "application/activity+json", "application/xhtml+xml"],
)
def test_url_source_accepts_public_document_media_types(content_type: str) -> None:
    report = build_health_report(
        {"review_records": source("review_records", url="https://culture.gov.taipei/News.aspx")},
        FakeClient([FakeResponse(200, content_type=content_type)]),
        previous_report=None,
        clock=lambda: NOW,
    )
    assert by_name(report)["review_records"]["status"] == "available"


@pytest.mark.parametrize("content_type", ["application/pdf", "application/csv"])
def test_configured_pruning_schedule_accepts_pdf_and_csv_media_types(
    content_type: str,
) -> None:
    report = build_health_report(
        {
            "pruning_schedule": source(
                "pruning_schedule",
                url="https://data.gov.taipei/pruning/schedule",
            )
        },
        FakeClient([FakeResponse(200, content_type=content_type)]),
        previous_report=None,
        clock=lambda: NOW,
    )

    assert by_name(report)["pruning_schedule"]["status"] == "available"


def test_unknown_source_without_locator_kind_fails_closed() -> None:
    with pytest.raises(
        health.HealthConfigurationError,
        match="source configuration is invalid",
    ):
        build_health_report(
            {"future_source": source("future_source")},
            FakeClient([]),
            previous_report=None,
            clock=lambda: NOW,
        )


@pytest.mark.parametrize("content_type", ["", "application/octet-stream", "application/notjson", "image/png"])
def test_url_source_rejects_unsafe_or_unexpected_media_types(content_type: str) -> None:
    report = build_health_report(
        {"review_records": source("review_records", url="https://culture.gov.taipei/News.aspx")},
        FakeClient([FakeResponse(200, content_type=content_type)]),
        previous_report=None,
        clock=lambda: NOW,
    )
    assert by_name(report)["review_records"]["status"] == "unavailable"


def test_safe_relative_redirect_is_followed_manually() -> None:
    client = FakeClient(
        [
            FakeResponse(302, location="/public/health", content_type="text/html"),
            FakeResponse(200, content_type="text/html"),
        ]
    )
    report = build_health_report(
        {"review_records": source("review_records", url="https://culture.gov.taipei/start")},
        client,
        previous_report=None,
        clock=lambda: NOW,
    )

    assert by_name(report)["review_records"]["status"] == "available"
    assert [call[0] for call in client.calls] == [
        "https://culture.gov.taipei/start",
        "https://culture.gov.taipei/public/health",
    ]
    assert all(call[1]["follow_redirects"] is False for call in client.calls)


@pytest.mark.parametrize(
    "location",
    [
        "https://example.invalid/file",
        "https://culture.gov.taipei/file?token=hidden",
        "http://culture.gov.taipei/file",
    ],
)
def test_unsafe_redirect_is_rejected_before_second_request(location: str) -> None:
    client = FakeClient([FakeResponse(302, location=location)])
    report = build_health_report(
        {"review_records": source("review_records", url="https://culture.gov.taipei/start")},
        client,
        previous_report=None,
        clock=lambda: NOW,
    )

    entry = by_name(report)["review_records"]
    assert entry["status"] == "unavailable"
    assert entry["reason"] == "redirect_rejected"
    assert len(client.calls) == 1
    assert "hidden" not in json.dumps(report)


def test_redirect_cycle_and_hop_count_are_bounded() -> None:
    cycle = FakeClient(
        [
            FakeResponse(302, location="/b"),
            FakeResponse(302, location="/a"),
        ]
    )
    cycle_report = build_health_report(
        {"review_records": source("review_records", url="https://culture.gov.taipei/a")},
        cycle,
        previous_report=None,
        clock=lambda: NOW,
    )
    assert by_name(cycle_report)["review_records"]["reason"] == "redirect_rejected"
    assert len(cycle.calls) == 2

    hops = FakeClient(
        [FakeResponse(302, location=f"/hop-{index + 1}") for index in range(6)]
    )
    hop_report = build_health_report(
        {"review_records": source("review_records", url="https://culture.gov.taipei/hop-0")},
        hops,
        previous_report=None,
        clock=lambda: NOW,
    )
    assert by_name(hop_report)["review_records"]["reason"] == "redirect_rejected"
    assert len(hops.calls) == 6


def write_config(path: Path, body: str | None = None) -> None:
    path.write_text(
        body
        or '{"street_trees":{"dataset_id":"dataset-id","required":true}}',
        encoding="utf-8",
    )


def test_cli_remote_unavailability_writes_report_and_exits_zero(tmp_path: Path) -> None:
    config = tmp_path / "sources.json"
    output = tmp_path / "reports" / "health.json"
    write_config(config)

    status = main(
        ["--config", str(config), "--out", str(output)],
        environ={},
        client_factory=lambda: FakeClient([FakeResponse(599) for _ in range(3)]),
        clock=lambda: NOW,
    )

    assert status == 0
    assert json.loads(output.read_text(encoding="utf-8"))["sources"][0]["status"] == "unavailable"


@pytest.mark.parametrize(
    ("config_body", "history_body"),
    [
        ('{"street_trees":{"dataset_id":"safe","dataset_id":"hidden","required":true}}', None),
        ('{"street_trees":{"dataset_id":"safe","required":"hidden"}}', None),
        (None, '{"schema_version":"1.0","schema_version":"hidden","generated_at":"x","sources":[]}'),
    ],
)
def test_cli_malformed_config_or_history_fails_with_fixed_safe_message(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    config_body: str | None,
    history_body: str | None,
) -> None:
    config = tmp_path / "sources.json"
    output = tmp_path / "health.json"
    write_config(config, config_body)
    if history_body is not None:
        output.write_text(history_body, encoding="utf-8")

    status = main(
        ["--config", str(config), "--out", str(output)],
        environ={},
        client_factory=lambda: FakeClient([]),
        clock=lambda: NOW,
    )

    captured = capsys.readouterr()
    assert status == 1
    assert captured.err == "健康報告設定、歷史資料或寫入失敗。\n"
    assert "hidden" not in captured.err


def test_cli_write_failure_and_type_error_are_fixed_and_safe(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config = tmp_path / "sources.json"
    write_config(config)

    def fail_write(*_args: object, **_kwargs: object) -> None:
        raise OSError("hidden-write-path")

    monkeypatch.setattr(health, "_write_report", fail_write)
    status = main(
        ["--config", str(config), "--out", str(tmp_path / "health.json")],
        environ={},
        client_factory=lambda: FakeClient([FakeResponse(200)]),
        clock=lambda: NOW,
    )

    assert status == 1
    assert capsys.readouterr().err == "健康報告設定、歷史資料或寫入失敗。\n"
