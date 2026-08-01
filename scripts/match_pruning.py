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
        if not unicodedata.category(character).startswith(("P", "Z")) and not character.isspace()
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
    street_records: list[tuple[str, str, str]] = []
    park_index: dict[tuple[str, str], list[str]] = {}
    for tree in trees:
        tree_id = tree.get("tree_id")
        tree_type = tree.get("tree_type")
        if not isinstance(tree_id, str) or tree_type not in {"street", "park"}:
            continue
        district = normalize_place(tree.get("district"))
        if tree_type == "street":
            location = normalize_place(tree.get("location"))
            street_records.append((tree_id, district, location))
        else:
            park_name = normalize_place(tree.get("park_name"))
            if district and park_name:
                park_index.setdefault((district, park_name), []).append(tree_id)
    matches: list[dict[str, str]] = []
    for schedule in schedules:
        schedule_id = schedule.get("schedule_id")
        category = schedule.get("category")
        if not isinstance(schedule_id, str) or category not in {"street", "park"}:
            continue
        districts = set(_values(schedule.get("districts")))
        locations = _values(schedule.get("locations"))
        if category == "street":
            for tree_id, district, tree_location in street_records:
                if districts and district not in districts:
                    continue
                if not tree_location or not any(
                    _usable_street_phrase(location) and location in tree_location
                    for location in locations
                ):
                    continue
                matches.append(
                    {
                        "schedule_id": schedule_id,
                        "tree_id": tree_id,
                        "match_method": "street_location_phrase",
                        "explanation": "依完整路段名稱比對，並非官方逐株施工名單",
                    }
                )
        elif districts:
            matched_tree_ids = {
                tree_id
                for district in districts
                for location in locations
                for tree_id in park_index.get((district, location), [])
            }
            matches.extend(
                {
                    "schedule_id": schedule_id,
                    "tree_id": tree_id,
                    "match_method": "park_district_and_name",
                    "explanation": "依行政區及完整公園名稱比對，並非官方逐株施工名單",
                }
                for tree_id in matched_tree_ids
            )
    return sorted(matches, key=lambda item: (item["schedule_id"], item["tree_id"]))
