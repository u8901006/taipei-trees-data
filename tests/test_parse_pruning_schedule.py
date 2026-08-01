from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

import scripts.fetch_schedule as fetcher

from scripts.parse_pruning_schedule import (
    ScheduleParseError,
    build_schedule_document,
    discover_schedule_urls,
    parse_schedule,
)


FIXTURES = Path(__file__).parent / "fixtures"
BASE_URL = (
    "https://pkl.gov.taipei/News.aspx?"
    "n=EBBD7C86561BDECF&sms=6C795C257A5AC781"
)
RETRIEVED_AT = datetime(2026, 8, 1, 0, 0, tzinfo=UTC)


def fixture(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def test_discovers_current_street_and_park_schedule_urls() -> None:
    urls = discover_schedule_urls(fixture("pruning_index.html"), BASE_URL)

    assert urls == {
        "street": "https://pkl.gov.taipei/News_Content.aspx?n=LIST&s=STREET",
        "park": "https://pkl.gov.taipei/News_Content.aspx?n=LIST&s=PARK",
    }


def test_parse_street_schedule_preserves_official_evidence_without_inferring_name() -> None:
    source_url = "https://pkl.gov.taipei/News_Content.aspx?n=LIST&s=STREET"

    schedules = parse_schedule(
        fixture("pruning_street.html"), "street", source_url, RETRIEVED_AT
    )

    assert schedules[0] == {
        "schedule_id": schedules[0]["schedule_id"],
        "category": "street",
        "start_date": "2026-07-28",
        "end_date": "2026-07-29",
        "districts": [],
        "locations": ["民生東路四段"],
        "team": "園藝一隊",
        "work_type": "修剪",
        "work_detail": "喬木修剪",
        "planned_count": 152,
        "work_unit": "海棠園藝有限公司",
        "basis": "里長建議",
        "requester_type": "village_chief_recommendation",
        "requester_name": None,
        "source_url": source_url,
        "published_at": None,
        "retrieved_at": RETRIEVED_AT.isoformat(),
    }
    assert schedules[1]["locations"] == ["瑞光路", "潭美街"]
    assert schedules[1]["planned_count"] == 12
    assert schedules[1]["requester_type"] == "councillor_case"
    assert schedules[1]["requester_name"] is None


def test_parse_park_schedule_uses_district_park_and_responsible_unit() -> None:
    source_url = "https://pkl.gov.taipei/News_Content.aspx?n=LIST&s=PARK"

    schedules = parse_schedule(fixture("pruning_park.html"), "park", source_url, RETRIEVED_AT)

    assert schedules[0]["category"] == "park"
    assert schedules[0]["start_date"] == schedules[0]["end_date"] == "2026-07-31"
    assert schedules[0]["districts"] == ["大安區"]
    assert schedules[0]["locations"] == ["大安森林公園"]
    assert schedules[0]["work_unit"] == "青年公園管理所"
    assert schedules[0]["basis"] is None
    assert schedules[0]["requester_name"] is None


def test_build_document_is_stably_sorted_and_versioned() -> None:
    street = parse_schedule(
        fixture("pruning_street.html"), "street", "https://pkl.gov.taipei/street", RETRIEVED_AT
    )
    park = parse_schedule(
        fixture("pruning_park.html"), "park", "https://pkl.gov.taipei/park", RETRIEVED_AT
    )

    document = build_schedule_document([*street, *park], RETRIEVED_AT)

    assert document["schema_version"] == 1
    assert document["retrieved_at"] == RETRIEVED_AT.isoformat()
    assert document["schedules"] == sorted(
        document["schedules"], key=lambda item: (item["start_date"], item["schedule_id"])
    )


def test_missing_required_headers_fail_closed() -> None:
    with pytest.raises(ScheduleParseError, match="schedule parse failed"):
        parse_schedule(
            "<table><tr><th>地點</th></tr><tr><td>仁愛路</td></tr></table>".encode(),
            "street",
            "https://pkl.gov.taipei/street",
            RETRIEVED_AT,
        )


def test_fetch_bundle_archives_evidence_and_atomically_writes_document(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    street_url = "https://pkl.gov.taipei/News_Content.aspx?n=LIST&s=STREET"
    park_url = "https://pkl.gov.taipei/News_Content.aspx?n=LIST&s=PARK"
    content = {
        BASE_URL: fixture("pruning_index.html"),
        street_url: fixture("pruning_street.html"),
        park_url: fixture("pruning_park.html"),
    }

    def fake_download(url: str, _client: object, _sleeper: object) -> tuple[str, str, bytes]:
        return url, "text/html", content[url]

    monkeypatch.setattr(fetcher, "_download", fake_download)
    processed = tmp_path / "processed" / "pruning_schedule.json"

    result = fetcher.fetch_schedule_bundle(
        BASE_URL,
        tmp_path / "raw",
        processed,
        object(),
        clock=lambda: RETRIEVED_AT,
        sleeper=lambda _delay: None,
    )

    assert result.schedule_count == 3
    assert result.new_files == 3
    assert len(result.paths) == 3
    assert all(path.exists() for path in result.paths)
    document = __import__("json").loads(processed.read_text(encoding="utf-8"))
    assert document["schema_version"] == 1
    assert len(document["schedules"]) == 3


def test_fetch_bundle_does_not_replace_last_valid_document_on_parse_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    processed = tmp_path / "processed" / "pruning_schedule.json"
    processed.parent.mkdir(parents=True)
    processed.write_text('{"last":"valid"}\n', encoding="utf-8")

    def fake_download(url: str, _client: object, _sleeper: object) -> tuple[str, str, bytes]:
        if url == BASE_URL:
            return url, "text/html", fixture("pruning_index.html")
        return url, "text/html", b"<html><table><tr><th>broken</th></tr></table></html>"

    monkeypatch.setattr(fetcher, "_download", fake_download)

    with pytest.raises(ScheduleParseError):
        fetcher.fetch_schedule_bundle(
            BASE_URL,
            tmp_path / "raw",
            processed,
            object(),
            clock=lambda: RETRIEVED_AT,
            sleeper=lambda _delay: None,
        )

    assert processed.read_text(encoding="utf-8") == '{"last":"valid"}\n'
