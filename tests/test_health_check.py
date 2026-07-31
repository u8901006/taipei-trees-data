from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

from scripts.config import SourceConfig
from scripts.health_check import HealthHistoryError, build_health_report


class FakeResponse:
    def __init__(self, status_code: int, *, content_type: str = "application/json") -> None:
        self.status_code = status_code
        self.headers = {"content-type": content_type}


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
            "z_optional": source("z_optional"),
            "a_optional": source("a_optional"),
        },
        FakeClient([]),
        previous_report=None,
        clock=lambda: NOW,
    )

    assert [entry["name"] for entry in report["sources"]] == ["a_optional", "z_optional"]  # type: ignore[index]
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
