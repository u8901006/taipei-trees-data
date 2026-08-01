"""Incrementally cache verifiable species photos from Wikimedia Commons."""

from __future__ import annotations

import argparse
import html
import json
import os
import re
import time
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote, unquote, urlsplit

import httpx


API_URL = "https://api.wikimedia.org/core/v1/commons/search/page"
WIKIPEDIA_API_URL = "https://zh.wikipedia.org/w/api.php"
TBN_TAXON_URL = "https://www.tbn.org.tw/api/v25/taxon"
DEFAULT_PREVIOUS_URL = "https://u8901006.github.io/taipei-trees-data/data/species_images.json"
_PHOTO_MIME_TYPES = {"image/jpeg", "image/png", "image/webp"}
_RETRYABLE_STATUS = {408, 429, 500, 502, 503, 504}
_TAG = re.compile(r"<[^>]+>")


def _plain_text(value: object, *, maximum: int = 300) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = " ".join(_TAG.sub(" ", html.unescape(value)).split())
    return cleaned[:maximum] or None


def _safe_url(value: object, host: str) -> str | None:
    if not isinstance(value, str):
        return None
    parsed = urlsplit(value)
    if (
        parsed.scheme != "https"
        or parsed.hostname != host
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
    ):
        return None
    return value


def _metadata_value(metadata: object, key: str) -> str | None:
    if not isinstance(metadata, Mapping):
        return None
    item = metadata.get(key)
    return _plain_text(item.get("value")) if isinstance(item, Mapping) else None


def compact_commons_result(
    species: str,
    query: str,
    payload: Mapping[str, object],
    now: datetime,
) -> dict[str, object] | None:
    """Select the first safe bitmap result and retain its public attribution."""
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("clock must be timezone-aware")
    query_document = payload.get("query")
    raw_pages = query_document.get("pages") if isinstance(query_document, Mapping) else None
    if isinstance(raw_pages, Mapping):
        pages = [page for page in raw_pages.values() if isinstance(page, Mapping)]
    elif isinstance(raw_pages, list):
        pages = [page for page in raw_pages if isinstance(page, Mapping)]
    else:
        pages = []
    pages.sort(key=lambda page: int(page.get("index", 1_000_000)))
    for page in pages:
        raw_information = page.get("imageinfo")
        if not isinstance(raw_information, list) or not raw_information:
            continue
        information = raw_information[0]
        if not isinstance(information, Mapping):
            continue
        if (
            information.get("mime") not in _PHOTO_MIME_TYPES
            or information.get("mediatype") != "BITMAP"
        ):
            continue
        image_url = _safe_url(
            information.get("thumburl") or information.get("url"), "upload.wikimedia.org"
        )
        source_page_url = _safe_url(information.get("descriptionurl"), "commons.wikimedia.org")
        if image_url is None or source_page_url is None:
            continue
        metadata = information.get("extmetadata")
        return {
            "species": species,
            "status": "available",
            "query": query,
            "image_url": image_url,
            "source_page_url": source_page_url,
            "license": _metadata_value(metadata, "LicenseShortName"),
            "artist": _metadata_value(metadata, "Artist"),
            "credit": _metadata_value(metadata, "Credit"),
            "retrieved_at": now.astimezone(UTC).isoformat(),
        }
    fallback = payload.get("wikipedia")
    fallback_query = fallback.get("query") if isinstance(fallback, Mapping) else None
    fallback_pages = fallback_query.get("pages") if isinstance(fallback_query, Mapping) else None
    if isinstance(fallback_pages, list):
        for page in fallback_pages:
            if not isinstance(page, Mapping) or page.get("missing") is True:
                continue
            thumbnail = page.get("thumbnail")
            image_url = (
                _safe_url(thumbnail.get("source"), "upload.wikimedia.org")
                if isinstance(thumbnail, Mapping)
                else None
            )
            source_page_url = _safe_url(page.get("fullurl"), "zh.wikipedia.org")
            if image_url is not None and source_page_url is not None:
                return {
                    "species": species,
                    "status": "available",
                    "query": query,
                    "image_url": image_url,
                    "source_page_url": source_page_url,
                    "license": None,
                    "artist": None,
                    "credit": "中文維基百科樹種條目縮圖；個別授權請見來源頁",
                    "retrieved_at": now.astimezone(UTC).isoformat(),
                }
    return None


def _records(previous: Mapping[str, object]) -> dict[str, dict[str, object]]:
    raw = previous.get("records") if previous.get("schema_version") == 1 else None
    if not isinstance(raw, Mapping):
        return {}
    return {
        species: dict(record)
        for species, record in raw.items()
        if isinstance(species, str)
        and isinstance(record, Mapping)
        and record.get("species") == species
        and record.get("status") in {"available", "unavailable", "pending"}
    }


def _profile_map(profiles: Sequence[Mapping[str, object]]) -> dict[str, Mapping[str, object]]:
    mapped: dict[str, Mapping[str, object]] = {}
    for profile in profiles:
        species = profile.get("species")
        if isinstance(species, str) and species.strip():
            mapped[species.strip()] = profile
    return mapped


def choose_species(
    profiles: Sequence[Mapping[str, object]], previous: Mapping[str, object], limit: int
) -> list[str]:
    """Refresh missing photos first, then rotate the oldest cached records."""
    if limit < 0:
        raise ValueError("limit cannot be negative")
    profile_by_species = _profile_map(profiles)
    records = _records(previous)

    def priority(species: str) -> tuple[int, str, str]:
        record = records.get(species)
        if record is None or record.get("status") == "pending":
            return (0, "", species)
        rank = 1 if record.get("status") == "unavailable" else 2
        return (rank, str(record.get("retrieved_at") or ""), species)

    ordered = sorted(profile_by_species, key=priority)
    return ordered if limit == 0 else ordered[:limit]


def species_query(profile: Mapping[str, object], species: str) -> str:
    scientific_name = profile.get("scientific_name")
    if isinstance(scientific_name, str) and scientific_name.strip():
        match = re.match(r"^([A-Z][A-Za-z-]+)\s+([a-z][A-Za-z-]+)", scientific_name.strip())
        if match:
            return f"{match.group(1)} {match.group(2)}"
    return species


def wikimedia_result_relevant(query: str, page: Mapping[str, object]) -> bool:
    """Reject same-genus and visually similar search results without an exact name match."""
    haystack = " ".join(
        _plain_text(page.get(field), maximum=2_000) or ""
        for field in ("key", "title", "description", "excerpt")
    ).casefold()
    latin = re.match(r"^([A-Z][A-Za-z-]+)\s+([a-z][A-Za-z-]+)", query.strip())
    if latin:
        return all(token.casefold() in haystack for token in latin.groups())
    normalized = re.sub(r"[\s()（）]+", "", query).casefold()
    normalized_haystack = re.sub(r"[\s()（）]+", "", haystack)
    return bool(normalized) and normalized in normalized_haystack


def refresh_species_images(
    profiles: Sequence[Mapping[str, object]],
    previous: Mapping[str, object],
    fetch_result: Callable[[str, str], Mapping[str, object]],
    *,
    limit: int = 600,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    sleeper: Callable[[float], None] = time.sleep,
    request_spacing: float = 0.12,
) -> dict[str, object]:
    """Refresh a bounded image rotation while retaining safe prior records."""
    now = clock()
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("clock must be timezone-aware")
    profile_by_species = _profile_map(profiles)
    records = _records(previous)
    selected = choose_species(profiles, previous, limit)
    errors: list[dict[str, str]] = []
    fetched = 0
    for index, species in enumerate(selected):
        query = species_query(profile_by_species[species], species)
        try:
            compact = compact_commons_result(species, query, fetch_result(species, query), now)
            records[species] = compact or {
                "species": species,
                "status": "unavailable",
                "query": query,
                "retrieved_at": now.astimezone(UTC).isoformat(),
            }
            fetched += 1
        except (httpx.HTTPError, RuntimeError, TypeError, ValueError):
            errors.append({"species": species, "error": "fetch_failed"})
            records.setdefault(
                species,
                {"species": species, "status": "pending", "query": query},
            )
        if index + 1 < len(selected) and request_spacing > 0:
            sleeper(request_spacing)
    for species in profile_by_species:
        records.setdefault(
            species,
            {
                "species": species,
                "status": "pending",
                "query": species_query(profile_by_species[species], species),
            },
        )
    records = {species: records[species] for species in sorted(profile_by_species)}
    return {
        "schema_version": 1,
        "generated_at": now.astimezone(UTC).isoformat(),
        "total_species": len(profile_by_species),
        "fetched_this_run": fetched,
        "available": sum(record.get("status") == "available" for record in records.values()),
        "records": records,
        "errors": errors,
    }


def _load_document(path: Path) -> dict[str, object]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("species profiles document is invalid")
    return value


def _load_previous(client: httpx.Client, url: str | None) -> dict[str, object]:
    if not url:
        return {"schema_version": 1, "records": {}}
    if _safe_url(url, "u8901006.github.io") is None:
        raise ValueError("previous image cache URL must be the official GitHub Pages URL")
    try:
        response = client.get(url, timeout=20.0, follow_redirects=False)
        if response.status_code == 404:
            return {"schema_version": 1, "records": {}}
        response.raise_for_status()
        value = response.json()
    except (httpx.HTTPError, ValueError):
        return {"schema_version": 1, "records": {}}
    return value if isinstance(value, dict) else {"schema_version": 1, "records": {}}


def _commons_fetcher(client: httpx.Client) -> Callable[[str, str], Mapping[str, object]]:
    def search_commons(query: str) -> Mapping[str, object] | None:
        response = client.get(
            API_URL,
            params={"q": query, "limit": 20},
            timeout=30.0,
            follow_redirects=False,
        )
        if response.status_code in _RETRYABLE_STATUS:
            raise RuntimeError("retryable Commons API response")
        response.raise_for_status()
        payload: Any = response.json()
        if not isinstance(payload, Mapping):
            raise ValueError("Commons API response must be an object")
        rest_pages = payload.get("pages")
        compact_pages: list[dict[str, object]] = []
        if isinstance(rest_pages, list):
            for index, page in enumerate(rest_pages):
                if not isinstance(page, Mapping) or not wikimedia_result_relevant(query, page):
                    continue
                thumbnail = page.get("thumbnail")
                if (
                    not isinstance(thumbnail, Mapping)
                    or thumbnail.get("mimetype") not in _PHOTO_MIME_TYPES
                ):
                    continue
                raw_thumbnail = thumbnail.get("url")
                if not isinstance(raw_thumbnail, str):
                    continue
                image_url = re.sub(r"/\d+px-", "/900px-", raw_thumbnail)
                key = str(page.get("key", ""))
                if not key.startswith("File:"):
                    path_parts = urlsplit(raw_thumbnail).path.split("/")
                    if len(path_parts) < 2:
                        continue
                    key = f"File:{unquote(path_parts[-2])}"
                compact_pages.append(
                    {
                        "index": index,
                        "imageinfo": [
                            {
                                "thumburl": image_url,
                                "descriptionurl": (
                                    f"https://commons.wikimedia.org/wiki/{quote(key, safe=':')}"
                                ),
                                "mime": thumbnail["mimetype"],
                                "mediatype": "BITMAP",
                                "extmetadata": {},
                            }
                        ],
                    }
                )
        compact_payload: dict[str, object] = {"query": {"pages": compact_pages}}
        if compact_commons_result(query, query, compact_payload, datetime.now(UTC)) is not None:
            return compact_payload
        return None

    def search_wikipedia(query: str) -> Mapping[str, object] | None:
        wikipedia_response = client.get(
            WIKIPEDIA_API_URL,
            params={
                "action": "query",
                "titles": query,
                "redirects": 1,
                "prop": "pageimages|info",
                "piprop": "thumbnail",
                "pithumbsize": 900,
                "inprop": "url",
                "format": "json",
                "formatversion": 2,
            },
            timeout=30.0,
            follow_redirects=False,
        )
        if wikipedia_response.status_code in _RETRYABLE_STATUS:
            raise RuntimeError("retryable Wikipedia API response")
        wikipedia_response.raise_for_status()
        wikipedia_payload: Any = wikipedia_response.json()
        if not isinstance(wikipedia_payload, Mapping):
            raise ValueError("Wikipedia API response must be an object")
        result = {"wikipedia": wikipedia_payload}
        return (
            result
            if compact_commons_result(query, query, result, datetime.now(UTC)) is not None
            else None
        )

    def tbn_scientific_name(species: str) -> str | None:
        try:
            response = client.get(
                TBN_TAXON_URL,
                params={"name": species},
                timeout=30.0,
                follow_redirects=False,
            )
            response.raise_for_status()
            payload: Any = response.json()
        except (httpx.HTTPError, ValueError):
            return None
        data = payload.get("data") if isinstance(payload, Mapping) else None
        if not isinstance(data, list):
            return None
        exact = [
            item
            for item in data
            if isinstance(item, Mapping)
            and item.get("vernacularName") == species
            and item.get("taxonRank") == "種"
            and isinstance(item.get("simplifiedScientificName"), str)
        ]
        names = {str(item["simplifiedScientificName"]).strip() for item in exact}
        return next(iter(names)) if len(names) == 1 else None

    def fetch(species: str, query: str) -> Mapping[str, object]:
        result = search_commons(query)
        if result is not None:
            return result
        standardized = tbn_scientific_name(species)
        if standardized and standardized != query:
            result = search_commons(standardized)
            if result is not None:
                return result
        result = search_wikipedia(species)
        if result is not None:
            return result
        if standardized and standardized != species:
            result = search_wikipedia(standardized)
            if result is not None:
                return result
        return {"query": {"pages": []}}

    return fetch


def _atomic_write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profiles", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--previous-url", default=DEFAULT_PREVIOUS_URL)
    parser.add_argument("--limit", type=int, default=600)
    arguments = parser.parse_args(argv)
    document = _load_document(arguments.profiles)
    profiles = document.get("profiles")
    if document.get("schema_version") != 1 or not isinstance(profiles, list):
        raise ValueError("species profiles document is invalid")
    with httpx.Client(
        headers={
            "Accept": "application/json",
            "User-Agent": "taipei-trees-data/1.0 (https://github.com/u8901006/taipei-trees-data)",
            "Api-User-Agent": (
                "taipei-trees-data/1.0 (https://github.com/u8901006/taipei-trees-data)"
            ),
        }
    ) as client:
        previous = _load_previous(client, arguments.previous_url)
        result = refresh_species_images(
            profiles,
            previous,
            _commons_fetcher(client),
            limit=arguments.limit,
        )
    _atomic_write_json(arguments.out, result)
    print(
        f"species images: {result['available']} available / {result['total_species']} species; "
        f"{len(result['errors'])} errors"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
