"""Conservatively match pruning schedule places to possible tree records."""

from __future__ import annotations

import unicodedata
from typing import Iterable


_DIRECTION_ONLY = frozenset(
    {
        "東側",
        "西側",
        "南側",
        "北側",
        "內側",
        "外側",
        "中央",
        "周邊",
        "附近",
        "兩側",
    }
)


def normalize_place(value: object) -> str:
    """Return a comparison-only place token without punctuation or spacing."""
    if value is None:
        return ""
    normalized = unicodedata.normalize("NFKC", str(value)).casefold().replace("台", "臺")
    return "".join(
        character
        for character in normalized
        if not unicodedata.category(character).startswith(("P", "Z"))
        and not character.isspace()
    )


def _values(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [normalized for item in value if (normalized := normalize_place(item))]


def _usable_street_phrase(value: str) -> bool:
    return len(value) >= 3 and value not in _DIRECTION_ONLY


def match_schedules(
    trees: Iterable[dict[str, object]], schedules: Iterable[dict[str, object]]
) -> list[dict[str, str]]:
    """Return evidence-labelled candidates without selecting by planned count."""
    tree_records = [dict(tree) for tree in trees]
    matches: list[dict[str, str]] = []
    for schedule in schedules:
        schedule_id = schedule.get("schedule_id")
        category = schedule.get("category")
        if not isinstance(schedule_id, str) or category not in {"street", "park"}:
            continue
        districts = set(_values(schedule.get("districts")))
        locations = _values(schedule.get("locations"))
        for tree in tree_records:
            tree_id = tree.get("tree_id")
            if not isinstance(tree_id, str) or tree.get("tree_type") != category:
                continue
            district = normalize_place(tree.get("district"))
            if districts and district not in districts:
                continue
            if category == "street":
                tree_location = normalize_place(tree.get("location"))
                if not tree_location or not any(
                    _usable_street_phrase(location) and location in tree_location
                    for location in locations
                ):
                    continue
                method = "street_location_phrase"
                explanation = "依完整路段名稱比對，並非官方逐株施工名單"
            else:
                if not districts:
                    continue
                park_name = normalize_place(tree.get("park_name"))
                if not park_name or park_name not in locations:
                    continue
                method = "park_district_and_name"
                explanation = "依行政區及完整公園名稱比對，並非官方逐株施工名單"
            matches.append(
                {
                    "schedule_id": schedule_id,
                    "tree_id": tree_id,
                    "match_method": method,
                    "explanation": explanation,
                }
            )
    return sorted(matches, key=lambda item: (item["schedule_id"], item["tree_id"]))
