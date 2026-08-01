"""Fail closed before publishing an incomplete public tree search index."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Sequence
from urllib.parse import urlsplit


def _read_json(path: Path) -> object:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise ValueError(f"invalid JSON: {path.name}") from error


def _official_https(value: object, allowed_hosts: set[str]) -> bool:
    if not isinstance(value, str):
        return False
    try:
        parsed = urlsplit(value)
    except ValueError:
        return False
    return (
        parsed.scheme == "https"
        and parsed.hostname in allowed_hosts
        and parsed.username is None
        and parsed.password is None
        and not parsed.fragment
    )


def validate_site_data(
    data_dir: Path,
    minimum_total: int,
    expected_districts: int,
    minimum_protected: int = 0,
) -> None:
    """Validate manifest thresholds and every referenced partition before deploy."""
    if minimum_total < 1 or expected_districts < 1 or minimum_protected < 0:
        raise ValueError("deployment thresholds must be positive")
    manifest = _read_json(data_dir / "manifest.json")
    if not isinstance(manifest, dict):
        raise ValueError("manifest must be an object")
    if manifest.get("schema_version") != 3:
        raise ValueError("manifest schema version is unsupported")
    districts = manifest.get("districts")
    if not isinstance(districts, list):
        raise ValueError("manifest districts must be a list")
    if manifest.get("total_count", 0) < minimum_total:
        raise ValueError("total_count is below the safe deployment threshold")
    if manifest.get("district_count") != expected_districts or len(districts) != expected_districts:
        raise ValueError("district count does not match the deployment contract")

    type_counts = manifest.get("type_counts")
    if (
        not isinstance(type_counts, dict)
        or set(type_counts) != {"street", "park", "protected"}
        or any(type(value) is not int or value < 0 for value in type_counts.values())
        or sum(type_counts.values()) != manifest.get("total_count")
    ):
        raise ValueError("tree type counts do not match total_count")
    if type_counts["protected"] < minimum_protected:
        raise ValueError("protected count is below the safe deployment threshold")

    coverage = manifest.get("protected_detail_coverage")
    coverage_keys = {"total", "available", "pending", "with_age", "with_photo", "with_story"}
    if (
        not isinstance(coverage, dict)
        or set(coverage) != coverage_keys
        or any(type(value) is not int or value < 0 for value in coverage.values())
        or coverage["total"] != type_counts["protected"]
        or coverage["available"] + coverage["pending"] != coverage["total"]
        or any(
            coverage[key] > coverage["total"] for key in ("with_age", "with_photo", "with_story")
        )
    ):
        raise ValueError("protected detail coverage is invalid")

    counted = 0
    tree_ids: set[str] = set()
    for entry in districts:
        if not isinstance(entry, dict) or not isinstance(entry.get("count"), int):
            raise ValueError("district entry is invalid")
        relative = entry.get("file")
        if not isinstance(relative, str):
            raise ValueError("district file is invalid")
        relative_path = Path(relative)
        if (
            relative_path.is_absolute()
            or len(relative_path.parts) != 2
            or relative_path.parts[0] != "districts"
            or relative_path.suffix != ".json"
        ):
            raise ValueError("district file must be a safe relative JSON path")
        records = _read_json(data_dir / relative_path)
        if not isinstance(records, list) or len(records) != entry["count"]:
            raise ValueError("district partition count does not match manifest count")
        if not records:
            raise ValueError("district partition must not be empty")
        for record in records:
            if not isinstance(record, dict) or not isinstance(record.get("id"), str):
                raise ValueError("tree record is invalid")
            if record["id"] in tree_ids:
                raise ValueError("tree id must be unique")
            if record.get("tree_type") == "protected":
                if record.get("detail_status") not in {"available", "pending"}:
                    raise ValueError("protected detail status is invalid")
                detail_url = record.get("official_detail_url")
                if not _official_https(detail_url, {"eculture.gov.taipei"}):
                    raise ValueError("protected detail URL is invalid")
                photo_url = record.get("photo_url")
                if photo_url is not None and not _official_https(
                    photo_url, {"ecultureuser.gov.taipei"}
                ):
                    raise ValueError("protected photo URL is invalid")
                if (
                    record.get("age_years") is not None or record.get("born_year") is not None
                ) and record.get("age_source") != "official_protected_tree_registry":
                    raise ValueError("protected age source is invalid")
            tree_ids.add(record["id"])
        counted += len(records)
    if counted != manifest["total_count"]:
        raise ValueError("partition count does not match total_count")

    schedule_file = manifest.get("schedule_file")
    matches_file = manifest.get("schedule_matches_file")
    if schedule_file != "schedules.json" or matches_file != "schedule_matches.json":
        raise ValueError("schedule files are invalid")
    schedule_document = _read_json(data_dir / schedule_file)
    if (
        not isinstance(schedule_document, dict)
        or schedule_document.get("schema_version") != 1
        or not isinstance(schedule_document.get("schedules"), list)
    ):
        raise ValueError("schedule document is invalid")
    schedule_ids: set[str] = set()
    for schedule in schedule_document["schedules"]:
        if not isinstance(schedule, dict) or not isinstance(schedule.get("schedule_id"), str):
            raise ValueError("schedule record is invalid")
        if schedule["schedule_id"] in schedule_ids:
            raise ValueError("schedule id must be unique")
        schedule_ids.add(schedule["schedule_id"])
    if manifest.get("schedule_count") != len(schedule_ids):
        raise ValueError("schedule count does not match manifest")
    for schedule in schedule_document["schedules"]:
        if schedule.get("requester_type") != "village_chief_recommendation" and any(
            schedule.get(field) is not None
            for field in (
                "village_leader_name",
                "village_leader_mobile",
                "village_leader_profile_url",
            )
        ):
            raise ValueError("leader contact is only valid for village-chief schedules")

    match_document = _read_json(data_dir / matches_file)
    if (
        not isinstance(match_document, dict)
        or match_document.get("schema_version") != 1
        or not isinstance(match_document.get("matches"), list)
    ):
        raise ValueError("schedule matches are invalid")
    for match in match_document["matches"]:
        if (
            not isinstance(match, dict)
            or match.get("schedule_id") not in schedule_ids
            or match.get("tree_id") not in tree_ids
        ):
            raise ValueError("schedule match references are invalid")

    profile_file = manifest.get("species_profile_file")
    if profile_file != "species_profiles.json":
        raise ValueError("species profile file is invalid")
    profile_document = _read_json(data_dir / profile_file)
    profiles = profile_document.get("profiles") if isinstance(profile_document, dict) else None
    if (
        not isinstance(profile_document, dict)
        or profile_document.get("schema_version") != 1
        or not isinstance(profiles, list)
        or manifest.get("species_profile_count") != len(profiles)
    ):
        raise ValueError("species profiles are invalid")
    seen_species: set[str] = set()
    for profile in profiles:
        if (
            not isinstance(profile, dict)
            or not isinstance(profile.get("species"), str)
            or profile["species"] in seen_species
            or type(profile.get("tree_count")) is not int
            or profile["tree_count"] < 1
        ):
            raise ValueError("species profile is invalid")
        links = profile.get("authoritative_links")
        if not isinstance(links, list) or any(
            not isinstance(link, dict)
            or not isinstance(link.get("url"), str)
            or not link["url"].startswith("https://")
            for link in links
        ):
            raise ValueError("species profile links are invalid")
        seen_species.add(profile["species"])

    image_document = _read_json(data_dir / "species_images.json")
    image_records = image_document.get("records") if isinstance(image_document, dict) else None
    if (
        not isinstance(image_document, dict)
        or image_document.get("schema_version") != 1
        or image_document.get("total_species") != len(seen_species)
        or not isinstance(image_records, dict)
        or set(image_records) != seen_species
        or not isinstance(image_document.get("errors"), list)
    ):
        raise ValueError("species image document is invalid")
    available = 0
    for species, record in image_records.items():
        if (
            not isinstance(record, dict)
            or record.get("species") != species
            or record.get("status") not in {"available", "unavailable", "pending"}
        ):
            raise ValueError("species image record is invalid")
        if record["status"] == "available":
            available += 1
            if not _official_https(record.get("image_url"), {"upload.wikimedia.org"}) or not (
                _official_https(
                    record.get("source_page_url"),
                    {"commons.wikimedia.org", "zh.wikipedia.org"},
                )
            ):
                raise ValueError("species image URL is invalid")
        elif record.get("image_url") is not None or record.get("source_page_url") is not None:
            raise ValueError("species image status is invalid")
    if image_document.get("available") != available:
        raise ValueError("species image coverage is invalid")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", required=True, type=Path)
    parser.add_argument("--minimum-total", required=True, type=int)
    parser.add_argument("--expected-districts", required=True, type=int)
    parser.add_argument("--minimum-protected", type=int, default=0)
    arguments = parser.parse_args(argv)
    try:
        validate_site_data(
            arguments.data,
            arguments.minimum_total,
            arguments.expected_districts,
            arguments.minimum_protected,
        )
    except ValueError as error:
        print(f"網站資料驗證失敗：{error}", file=sys.stderr)
        return 1
    print("網站資料通過發布安全閘門。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
