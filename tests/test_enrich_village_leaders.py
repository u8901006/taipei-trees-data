from __future__ import annotations

from pathlib import Path

from scripts.enrich_village_leaders import (
    enrich_schedules,
    parse_leader_profile,
    resolve_schedule_village,
)


OFFICIAL_URL = (
    "https://li.taipei/News_Content_VillageLeader.aspx?"
    "n=99B6C1D6A9596E73&sms=D982815F3A372FF5&s=04A7C7CC05945111"
)
PARK_SOURCE = "https://ssdo.gov.taipei/News.aspx?n=168EE47B876839FB&sms=3F9632016583341A"
FIXTURE = Path(__file__).parent / "fixtures" / "village_leader_dongrong.html"
CROSSWALK = {
    "schema_version": 1,
    "parks": [
        {
            "park_name": "富錦一號公園",
            "district": "松山區",
            "villages": ["東榮里"],
            "match_method": "manual_verified",
            "source_url": PARK_SOURCE,
            "verified_at": "2026-08-02",
            "leader_profile_url": OFFICIAL_URL,
        }
    ],
}
FUJIN_SCHEDULE = {
    "schedule_id": "schedule-1",
    "category": "park",
    "districts": ["松山區"],
    "locations": ["富錦一號公園"],
    "requester_type": "village_chief_recommendation",
    "requester_name": None,
}


def test_parse_dongrong_leader_profile() -> None:
    profile = parse_leader_profile(FIXTURE.read_text(encoding="utf-8"), OFFICIAL_URL)

    assert profile.district == "松山區"
    assert profile.village == "東榮里"
    assert profile.name == "鄭玉梅"
    assert profile.mobile == "0933902948"
    assert profile.profile_url == OFFICIAL_URL
    assert profile.source_updated_at == "112-02-24 10:20"


def test_fujin_park_crosswalk_has_official_source_evidence() -> None:
    match = resolve_schedule_village(FUJIN_SCHEDULE, CROSSWALK)

    assert match.villages == ("東榮里",)
    assert match.method == "manual_verified"
    assert match.source_url == PARK_SOURCE
    assert match.verified_at == "2026-08-02"
    assert match.status == "verified"


def test_crosswalk_does_not_fuzzy_match_wrong_village_or_park() -> None:
    schedule = {**FUJIN_SCHEDULE, "locations": ["富錦十號公園"]}

    match = resolve_schedule_village(schedule, CROSSWALK)

    assert match.status == "unresolved"
    assert match.villages == ()


def test_cross_village_park_is_not_assigned_to_one_leader() -> None:
    crosswalk = {
        "schema_version": 1,
        "parks": [
            {
                **CROSSWALK["parks"][0],
                "villages": ["東榮里", "三民里"],
            }
        ],
    }

    match = resolve_schedule_village(FUJIN_SCHEDULE, crosswalk)

    assert match.status == "cross_village"
    assert match.villages == ("東榮里", "三民里")


def test_enrich_village_chief_schedule_adds_current_official_leader() -> None:
    document = {
        "schema_version": 1,
        "retrieved_at": "2026-08-02T00:00:00Z",
        "schedules": [FUJIN_SCHEDULE],
    }

    enriched = enrich_schedules(
        document,
        CROSSWALK,
        lambda url: FIXTURE.read_text(encoding="utf-8"),
    )

    schedule = enriched["schedules"][0]
    assert schedule["village"] == "東榮里"
    assert schedule["village_match_method"] == "manual_verified"
    assert schedule["village_match_source_url"] == PARK_SOURCE
    assert schedule["village_leader_name"] == "鄭玉梅"
    assert schedule["village_leader_mobile"] == "0933902948"
    assert schedule["village_leader_profile_url"] == OFFICIAL_URL


def test_non_village_chief_schedule_never_gets_leader_contact() -> None:
    schedule = {**FUJIN_SCHEDULE, "requester_type": "councillor_case"}
    document = {"schema_version": 1, "retrieved_at": None, "schedules": [schedule]}

    enriched = enrich_schedules(
        document,
        CROSSWALK,
        lambda url: FIXTURE.read_text(encoding="utf-8"),
    )

    result = enriched["schedules"][0]
    assert result["village_leader_name"] is None
    assert result["village_leader_mobile"] is None
    assert result["village_leader_profile_url"] is None
