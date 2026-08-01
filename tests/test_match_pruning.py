from __future__ import annotations

from scripts.match_pruning import match_schedules, normalize_place


def test_normalize_place_handles_unicode_spacing_punctuation_and_tai_variant() -> None:
    assert normalize_place(" 台 北 路， 一段 ") == "臺北路一段"


def test_street_schedule_matches_every_full_location_candidate_without_count_truncation() -> None:
    trees = [
        {
            "tree_id": "S-1",
            "tree_type": "street",
            "district": "松山區",
            "location": "民生東路四段 100 號前",
        },
        {
            "tree_id": "S-2",
            "tree_type": "street",
            "district": "松山區",
            "location": "民生東路四段與光復北路口",
        },
    ]
    schedules = [
        {
            "schedule_id": "schedule-1",
            "category": "street",
            "districts": ["松山區"],
            "locations": ["民生東路四段"],
            "planned_count": 1,
            "requester_name": None,
        }
    ]

    matches = match_schedules(trees, schedules)

    assert matches == [
        {
            "schedule_id": "schedule-1",
            "tree_id": "S-1",
            "match_method": "street_location_phrase",
            "explanation": "依完整路段名稱比對，並非官方逐株施工名單",
        },
        {
            "schedule_id": "schedule-1",
            "tree_id": "S-2",
            "match_method": "street_location_phrase",
            "explanation": "依完整路段名稱比對，並非官方逐株施工名單",
        },
    ]
    assert schedules[0]["requester_name"] is None


def test_street_schedule_rejects_short_direction_and_district_mismatch() -> None:
    trees = [
        {"tree_id": "S-1", "tree_type": "street", "district": "信義區", "location": "北側"},
        {
            "tree_id": "S-2",
            "tree_type": "street",
            "district": "信義區",
            "location": "仁愛路四段",
        },
    ]
    schedules = [
        {
            "schedule_id": "short",
            "category": "street",
            "districts": [],
            "locations": ["北側"],
        },
        {
            "schedule_id": "wrong-district",
            "category": "street",
            "districts": ["大安區"],
            "locations": ["仁愛路四段"],
        },
    ]

    assert match_schedules(trees, schedules) == []


def test_park_schedule_requires_exact_normalized_park_and_district() -> None:
    trees = [
        {
            "tree_id": "P-1",
            "tree_type": "park",
            "district": "大安區",
            "park_name": "大安森林公園",
        },
        {
            "tree_id": "P-2",
            "tree_type": "park",
            "district": "中山區",
            "park_name": "大安森林公園",
        },
        {
            "tree_id": "P-3",
            "tree_type": "park",
            "district": "大安區",
            "park_name": "大安公園",
        },
    ]
    schedules = [
        {
            "schedule_id": "park-1",
            "category": "park",
            "districts": ["大安區"],
            "locations": ["大安森林公園"],
        }
    ]

    assert match_schedules(trees, schedules) == [
        {
            "schedule_id": "park-1",
            "tree_id": "P-1",
            "match_method": "park_district_and_name",
            "explanation": "依行政區及完整公園名稱比對，並非官方逐株施工名單",
        }
    ]


def test_park_schedule_without_district_is_not_matched() -> None:
    trees = [
        {
            "tree_id": "P-1",
            "tree_type": "park",
            "district": "大安區",
            "park_name": "大安森林公園",
        }
    ]
    schedules = [
        {
            "schedule_id": "park-1",
            "category": "park",
            "districts": [],
            "locations": ["大安森林公園"],
        }
    ]

    assert match_schedules(trees, schedules) == []
