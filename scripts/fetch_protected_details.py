"""Incrementally cache compact official details for Taipei protected trees."""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import time
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlsplit

import httpx
import pandas as pd


API_ROOT = "https://ecultureuser.gov.taipei/data/api/tree"
DETAIL_PAGE_ROOT = "https://eculture.gov.taipei/trees/zh-tw/tree"
DEFAULT_PREVIOUS_URL = (
    "https://u8901006.github.io/taipei-trees-data/data/protected_tree_details.json"
)
OFFICIAL_IMAGE_HOST = "ecultureuser.gov.taipei"
_RETRYABLE_STATUS = frozenset({408, 429, 500, 502, 503, 504})


def _clean_text(value: object) -> str | None:
    if value is None:
        return None
    cleaned = str(value).strip()
    return cleaned or None


def _clean_int(value: object) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        result = int(value)
    except (TypeError, ValueError):
        return None
    return result if result >= 0 else None


def _clean_iso(value: object) -> str | None:
    cleaned = _clean_text(value)
    if cleaned is None:
        return None
    try:
        datetime.fromisoformat(cleaned.replace("Z", "+00:00"))
    except ValueError:
        return None
    return cleaned


def _official_image_url(value: object) -> str | None:
    cleaned = _clean_text(value)
    if cleaned is None:
        return None
    try:
        parsed = urlsplit(cleaned)
    except ValueError:
        return None
    if parsed.scheme != "https" or parsed.hostname != OFFICIAL_IMAGE_HOST:
        return None
    if parsed.username is not None or parsed.password is not None or parsed.fragment:
        return None
    return cleaned


def _valid_images(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    urls: list[str] = []
    for image in value:
        if not isinstance(image, Mapping):
            continue
        transform = image.get("transform")
        candidates: list[object] = []
        if isinstance(transform, Mapping):
            candidates.extend((transform.get("comp"), transform.get("original")))
        candidates.append(image.get("url"))
        selected = next((url for item in candidates if (url := _official_image_url(item))), None)
        if selected is not None:
            urls.append(selected)
    return urls


def _detail_url(code: str) -> str:
    return f"{DETAIL_PAGE_ROOT}/{quote(code, safe='')}"


def compact_detail(payload: Mapping[str, object], fetched_at: datetime) -> dict[str, object]:
    """Reduce an official API response to fields safe and useful for the static site."""
    if fetched_at.tzinfo is None or fetched_at.utcoffset() is None:
        raise ValueError("fetched_at must be timezone-aware")
    code = _clean_text(payload.get("code"))
    if code is None:
        raise ValueError("official detail response is missing code")
    images = _valid_images(payload.get("images"))
    return {
        "code": code,
        "district": _clean_text(payload.get("divisionName")),
        "village": _clean_text(payload.get("villageName")),
        "age_years": _clean_int(payload.get("age")),
        "born_year": _clean_int(payload.get("bornYear")),
        "photo_url": images[0] if images else None,
        "photo_count": len(images),
        "story": _clean_text(payload.get("historyInfo")),
        "environment_description": _clean_text(payload.get("envDescription")),
        "official_modified_at": _clean_iso(payload.get("modifyDate")),
        "official_detail_url": _detail_url(code),
        "detail_status": "available",
        "detail_fetched_at": fetched_at.astimezone(UTC).isoformat(),
    }


def _records(cache: Mapping[str, object]) -> dict[str, dict[str, object]]:
    raw_records = cache.get("records")
    if not isinstance(raw_records, Mapping):
        return {}
    records: dict[str, dict[str, object]] = {}
    for raw_code, raw_record in raw_records.items():
        if isinstance(raw_code, str) and isinstance(raw_record, Mapping):
            records[raw_code] = dict(raw_record)
    return records


def choose_codes(codes: Sequence[str], cache: Mapping[str, object], limit: int) -> list[str]:
    """Choose missing records first, followed by the oldest cached records."""
    if limit < 0:
        raise ValueError("limit cannot be negative")
    unique_codes = sorted({str(code).strip() for code in codes if str(code).strip()})
    records = _records(cache)

    def priority(code: str) -> tuple[int, str, str]:
        record = records.get(code)
        fetched_at = record.get("detail_fetched_at") if record else None
        missing = record is None or not isinstance(fetched_at, str) or not fetched_at.strip()
        return (0 if missing else 1, "" if missing else fetched_at, code)

    ordered = sorted(unique_codes, key=priority)
    return ordered if limit == 0 else ordered[:limit]


def refresh_details(
    codes: Sequence[str],
    previous: Mapping[str, object],
    fetch_detail: Callable[[str], Mapping[str, object]],
    *,
    limit: int = 300,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    sleeper: Callable[[float], None] = time.sleep,
    request_spacing: float = 0.15,
) -> dict[str, object]:
    """Refresh a bounded rotation while preserving every usable prior record."""
    now = clock()
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("clock must be timezone-aware")
    normalized_codes = sorted({str(code).strip() for code in codes if str(code).strip()})
    records = _records(previous)
    selected = choose_codes(normalized_codes, previous, limit)
    errors: list[dict[str, str]] = []
    fetched = 0
    for index, code in enumerate(selected):
        succeeded = False
        for attempt in range(3):
            try:
                payload = fetch_detail(code)
                detail = compact_detail(payload, now)
                if detail["code"] != code:
                    raise ValueError("official detail code does not match request")
                records[code] = detail
                fetched += 1
                succeeded = True
                break
            except (httpx.HTTPError, RuntimeError, ValueError, TypeError):
                if attempt < 2:
                    sleeper(0.5 * (2**attempt))
        if not succeeded:
            errors.append({"code": code, "error": "fetch_failed"})
        if index + 1 < len(selected) and request_spacing > 0:
            sleeper(request_spacing)

    for code in normalized_codes:
        records.setdefault(
            code,
            {
                "code": code,
                "detail_status": "pending",
                "official_detail_url": _detail_url(code),
            },
        )
    records = {code: records[code] for code in normalized_codes}
    return {
        "schema_version": 1,
        "generated_at": now.astimezone(UTC).isoformat(),
        "total_codes": len(normalized_codes),
        "fetched_this_run": fetched,
        "records": records,
        "errors": errors,
    }


def _load_previous(client: httpx.Client, url: str | None) -> dict[str, object]:
    if not url:
        return {"schema_version": 1, "records": {}}
    parsed = urlsplit(url)
    if (
        parsed.scheme != "https"
        or parsed.hostname != "u8901006.github.io"
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise ValueError("previous cache URL must be the official GitHub Pages HTTPS URL")
    try:
        response = client.get(url, timeout=20.0, follow_redirects=False)
        if response.status_code == 404:
            return {"schema_version": 1, "records": {}}
        response.raise_for_status()
        value = response.json()
    except (httpx.HTTPError, ValueError):
        return {"schema_version": 1, "records": {}}
    if not isinstance(value, dict) or value.get("schema_version") != 1:
        return {"schema_version": 1, "records": {}}
    return value


def _http_fetcher(client: httpx.Client) -> Callable[[str], Mapping[str, object]]:
    def fetch(code: str) -> Mapping[str, object]:
        response = client.get(
            f"{API_ROOT}/{quote(code, safe='')}", timeout=30.0, follow_redirects=False
        )
        if response.status_code in _RETRYABLE_STATUS:
            raise RuntimeError("retryable official API response")
        response.raise_for_status()
        payload: Any = response.json()
        if not isinstance(payload, dict):
            raise ValueError("official detail response must be an object")
        return payload

    return fetch


def _atomic_write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    content = (
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode("utf-8")
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
    parser.add_argument("--src", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--previous-url", default=DEFAULT_PREVIOUS_URL)
    parser.add_argument("--limit", type=int, default=300)
    arguments = parser.parse_args(argv)
    try:
        frame = pd.read_parquet(arguments.src, columns=["tree_id"])
        codes = [str(value).strip() for value in frame["tree_id"].dropna().tolist()]
        with httpx.Client(headers={"Accept": "application/json"}) as client:
            previous = _load_previous(client, arguments.previous_url)
            document = refresh_details(
                codes,
                previous,
                _http_fetcher(client),
                limit=arguments.limit,
            )
        _atomic_write_json(arguments.out, document)
    except (OSError, ValueError, httpx.HTTPError) as error:
        print(f"protected detail refresh failed: {type(error).__name__}", file=sys.stderr)
        return 1
    print(
        f"protected details: {document['fetched_this_run']} fetched; "
        f"{len(document['errors'])} errors"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
