from __future__ import annotations

from datetime import UTC, datetime

from scripts.fetch_protected_details import choose_codes, compact_detail, refresh_details


FIXED_TIME = datetime(2026, 8, 2, 3, 4, 5, tzinfo=UTC)


def _payload(code: str = "668") -> dict[str, object]:
    return {
        "code": code,
        "divisionName": "大安區",
        "villageName": "德安里",
        "age": 55,
        "bornYear": 1971,
        "historyInfo": "樹木的歷史故事。",
        "envDescription": "樹木生長環境。",
        "modifyDate": "2026-07-23T16:52:21.385612",
        "images": [
            {
                "url": "https://ecultureuser.gov.taipei/upload/image/668.jpg",
                "transform": {"comp": "https://ecultureuser.gov.taipei/upload/image/668_comp.jpg"},
            },
            {"url": "https://ecultureuser.gov.taipei/upload/image/668-2.jpg"},
        ],
    }


def test_compact_detail_keeps_official_age_photo_story_and_source() -> None:
    result = compact_detail(_payload(), FIXED_TIME)

    assert result == {
        "code": "668",
        "district": "大安區",
        "village": "德安里",
        "age_years": 55,
        "born_year": 1971,
        "photo_url": "https://ecultureuser.gov.taipei/upload/image/668_comp.jpg",
        "photo_count": 2,
        "story": "樹木的歷史故事。",
        "environment_description": "樹木生長環境。",
        "official_modified_at": "2026-07-23T16:52:21.385612",
        "official_detail_url": "https://eculture.gov.taipei/trees/zh-tw/tree/668",
        "detail_status": "available",
        "detail_fetched_at": "2026-08-02T03:04:05+00:00",
    }


def test_compact_detail_marks_fetched_missing_fields_as_available() -> None:
    result = compact_detail({"code": "4418", "images": []}, FIXED_TIME)

    assert result["detail_status"] == "available"
    assert result["age_years"] is None
    assert result["born_year"] is None
    assert result["photo_url"] is None
    assert result["photo_count"] == 0
    assert result["story"] is None


def test_compact_detail_rejects_non_official_or_non_https_images() -> None:
    payload = _payload()
    payload["images"] = [
        {"url": "http://ecultureuser.gov.taipei/image.jpg"},
        {"url": "https://evil.example/image.jpg"},
        {"url": "javascript:alert(1)"},
    ]

    result = compact_detail(payload, FIXED_TIME)

    assert result["photo_url"] is None
    assert result["photo_count"] == 0


def test_choose_codes_prioritizes_missing_then_oldest_cached() -> None:
    cache = {
        "records": {
            "2": {"detail_fetched_at": "2026-08-02T00:00:00+00:00"},
            "3": {"detail_fetched_at": "2026-07-01T00:00:00+00:00"},
            "4": {"detail_fetched_at": None},
        }
    }

    assert choose_codes(["1", "2", "3", "4"], cache, 3) == ["1", "4", "3"]
    assert choose_codes(["1", "2", "3", "4"], cache, 0) == ["1", "4", "3", "2"]


def test_refresh_details_preserves_old_record_when_fetch_fails() -> None:
    previous_record = compact_detail(_payload("668"), FIXED_TIME)
    previous = {"schema_version": 1, "records": {"668": previous_record}}

    def failing_fetch(code: str) -> dict[str, object]:
        raise RuntimeError(f"unavailable {code}")

    document = refresh_details(
        ["668"],
        previous,
        failing_fetch,
        limit=300,
        clock=lambda: datetime(2026, 8, 3, tzinfo=UTC),
        sleeper=lambda _: None,
        request_spacing=0,
    )

    assert document["records"]["668"] == previous_record
    assert document["fetched_this_run"] == 0
    assert document["errors"] == [{"code": "668", "error": "fetch_failed"}]


def test_refresh_details_adds_pending_records_outside_run_limit() -> None:
    document = refresh_details(
        ["1", "2"],
        {"schema_version": 1, "records": {}},
        lambda code: _payload(code),
        limit=1,
        clock=lambda: FIXED_TIME,
        sleeper=lambda _: None,
        request_spacing=0,
    )

    assert document["records"]["1"]["detail_status"] == "available"
    assert document["records"]["2"] == {
        "code": "2",
        "detail_status": "pending",
        "official_detail_url": "https://eculture.gov.taipei/trees/zh-tw/tree/2",
    }
    assert document["total_codes"] == 2
    assert document["fetched_this_run"] == 1
