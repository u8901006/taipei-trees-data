"""Enrich pruning schedules with source-backed villages and public leader contacts."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
import unicodedata
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from bs4 import BeautifulSoup


_ALLOWED_MATCH_METHODS = frozenset(
    {"source", "address", "coordinate", "park_crosswalk", "manual_verified", "unresolved"}
)


@dataclass(frozen=True, slots=True)
class VillageLeader:
    district: str
    village: str
    name: str
    mobile: str
    profile_url: str
    source_updated_at: str | None


@dataclass(frozen=True, slots=True)
class VillageMatch:
    villages: tuple[str, ...]
    method: str
    source_url: str | None
    verified_at: str | None
    status: str
    district: str | None = None
    leader_profile_url: str | None = None


def _clean(value: object) -> str:
    return re.sub(r"\s+", " ", unicodedata.normalize("NFKC", str(value))).strip()


def _official_url(value: object, *, host: str | None = None) -> str | None:
    cleaned = _clean(value)
    try:
        parsed = urlsplit(cleaned)
    except (TypeError, ValueError):
        return None
    hostname = parsed.hostname.casefold() if parsed.hostname else ""
    official_host = hostname == "gov.taipei" or hostname.endswith(".gov.taipei")
    if host == "li.taipei" and hostname == host:
        official_host = True
    if (
        parsed.scheme != "https"
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
        or not official_host
        or (host is not None and hostname != host)
    ):
        return None
    return cleaned


def parse_leader_profile(html: str | bytes, profile_url: str) -> VillageLeader:
    """Parse one official Taipei Neighborhood Service leader profile."""
    official_profile = _official_url(profile_url, host="li.taipei")
    if official_profile is None:
        raise ValueError("leader profile URL must be official HTTPS")
    if isinstance(html, bytes):
        try:
            html = html.decode("utf-8-sig")
        except UnicodeDecodeError as error:
            raise ValueError("leader profile encoding is invalid") from error
    soup = BeautifulSoup(html, "html.parser")
    text = _clean(soup.get_text(" ", strip=True))

    district: str | None = None
    village: str | None = None
    name: str | None = None
    for image in soup.find_all("img", alt=True):
        match = re.search(
            r"(?P<district>[\u4e00-\u9fff]{1,4}區)"
            r"(?P<village>[\u4e00-\u9fff]{1,4}里)"
            r"(?P<name>[\u4e00-\u9fff]{2,4})里長",
            _clean(image.get("alt", "")),
        )
        if match is not None:
            district = match.group("district")
            village = match.group("village")
            name = match.group("name")
            break
    maintenance = re.search(
        r"資料維護[：:]\s*臺北市(?P<district>[\u4e00-\u9fff]{1,4}區)"
        r"(?P<village>[\u4e00-\u9fff]{1,4}里)",
        text,
    )
    if maintenance is not None:
        district = district or maintenance.group("district")
        village = village or maintenance.group("village")
    if name is None:
        name_match = re.search(r"(?:^|\s)([\u4e00-\u9fff]{2,4})\s*里長(?:\s|$)", text)
        name = name_match.group(1) if name_match is not None else None
    mobile_match = re.search(r"里長行動電話[：:]\s*([0-9()\-\s]{8,20})", text)
    mobile = re.sub(r"\D", "", mobile_match.group(1)) if mobile_match else ""
    updated_match = re.search(r"資料更新[：:]\s*([^ ]+(?:\s+\d{1,2}:\d{2})?)", text)
    updated = _clean(updated_match.group(1)) if updated_match else None
    if not district or not village or not name or not re.fullmatch(r"09\d{8}", mobile):
        raise ValueError("official leader profile is missing required public fields")
    return VillageLeader(district, village, name, mobile, official_profile, updated)


def _unresolved() -> VillageMatch:
    return VillageMatch((), "unresolved", None, None, "unresolved")


def resolve_schedule_village(
    schedule: Mapping[str, object], crosswalk: Mapping[str, object]
) -> VillageMatch:
    """Resolve only exact, evidence-backed park names; never fuzzy match."""
    raw_parks = crosswalk.get("parks")
    if crosswalk.get("schema_version") != 1 or not isinstance(raw_parks, list):
        raise ValueError("park village crosswalk is invalid")
    locations = {
        _clean(value) for value in schedule.get("locations", []) if isinstance(value, str) and _clean(value)
    }
    districts = {
        _clean(value) for value in schedule.get("districts", []) if isinstance(value, str) and _clean(value)
    }
    candidates: list[Mapping[str, object]] = []
    for raw in raw_parks:
        if not isinstance(raw, Mapping):
            raise ValueError("park village crosswalk is invalid")
        park_name = _clean(raw.get("park_name", ""))
        district = _clean(raw.get("district", ""))
        if park_name in locations and (not districts or district in districts):
            candidates.append(raw)
    if len(candidates) != 1:
        return _unresolved()
    entry = candidates[0]
    method = _clean(entry.get("match_method", ""))
    source_url = _official_url(entry.get("source_url"))
    verified_at = _clean(entry.get("verified_at", "")) or None
    district = _clean(entry.get("district", "")) or None
    profile_url = _official_url(entry.get("leader_profile_url"), host="li.taipei")
    raw_villages = entry.get("villages")
    if (
        method not in _ALLOWED_MATCH_METHODS - {"unresolved"}
        or source_url is None
        or verified_at is None
        or district is None
        or not isinstance(raw_villages, list)
        or not raw_villages
    ):
        raise ValueError("park village crosswalk entry is invalid")
    villages = tuple(dict.fromkeys(_clean(value) for value in raw_villages if _clean(value)))
    if not villages or any(not village.endswith("里") for village in villages):
        raise ValueError("park village crosswalk entry is invalid")
    status = "verified" if len(villages) == 1 else "cross_village"
    return VillageMatch(
        villages,
        method,
        source_url,
        verified_at,
        status,
        district,
        profile_url,
    )


def enrich_schedules(
    document: Mapping[str, object],
    crosswalk: Mapping[str, object],
    profile_loader: Callable[[str], str | bytes],
) -> dict[str, object]:
    """Attach village evidence and current leader contacts to schedule records."""
    schedules = document.get("schedules")
    if document.get("schema_version") != 1 or not isinstance(schedules, list):
        raise ValueError("schedule document is invalid")
    profiles: dict[str, VillageLeader | None] = {}
    enriched: list[dict[str, object]] = []
    for raw_schedule in schedules:
        if not isinstance(raw_schedule, Mapping):
            raise ValueError("schedule document is invalid")
        schedule = dict(raw_schedule)
        match = resolve_schedule_village(schedule, crosswalk)
        schedule.update(
            {
                "village": "、".join(match.villages) if match.villages else None,
                "village_match_status": match.status,
                "village_match_method": match.method,
                "village_match_source_url": match.source_url,
                "village_verified_at": match.verified_at,
                "village_leader_name": None,
                "village_leader_mobile": None,
                "village_leader_profile_url": None,
                "village_leader_source_updated_at": None,
            }
        )
        if (
            schedule.get("requester_type") == "village_chief_recommendation"
            and match.status == "verified"
            and match.leader_profile_url is not None
        ):
            if match.leader_profile_url not in profiles:
                try:
                    profiles[match.leader_profile_url] = parse_leader_profile(
                        profile_loader(match.leader_profile_url), match.leader_profile_url
                    )
                except (httpx.HTTPError, OSError, ValueError):
                    profiles[match.leader_profile_url] = None
            profile = profiles[match.leader_profile_url]
            if (
                profile is not None
                and profile.district == match.district
                and profile.village == match.villages[0]
            ):
                schedule.update(
                    {
                        "village_leader_name": profile.name,
                        "village_leader_mobile": profile.mobile,
                        "village_leader_profile_url": profile.profile_url,
                        "village_leader_source_updated_at": profile.source_updated_at,
                    }
                )
        enriched.append(schedule)
    return {
        "schema_version": 1,
        "retrieved_at": document.get("retrieved_at"),
        "schedules": enriched,
    }


def _atomic_write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    content = (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode(
        "utf-8"
    )
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb", dir=path.parent, prefix=f".{path.name}.", delete=False
        ) as output:
            temporary = Path(output.name)
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--schedule", required=True, type=Path)
    parser.add_argument("--crosswalk", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    arguments = parser.parse_args(argv)
    try:
        document = json.loads(arguments.schedule.read_text(encoding="utf-8"))
        crosswalk = json.loads(arguments.crosswalk.read_text(encoding="utf-8"))
        with httpx.Client(headers={"Accept": "text/html"}) as client:
            def load_profile(url: str) -> str:
                response = client.get(url, timeout=30.0, follow_redirects=False)
                response.raise_for_status()
                return response.text

            enriched = enrich_schedules(document, crosswalk, load_profile)
        _atomic_write_json(arguments.out, enriched)
    except (OSError, UnicodeError, ValueError, httpx.HTTPError) as error:
        print(f"village leader enrichment failed: {type(error).__name__}", file=sys.stderr)
        return 1
    print(f"village leader enrichment: {len(enriched['schedules'])} schedules")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
