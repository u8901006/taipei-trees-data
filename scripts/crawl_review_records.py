"""Archive official Taipei tree-review and committee meeting-record PDFs."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import unicodedata
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Literal, Sequence
from urllib.parse import parse_qsl, urljoin, urlsplit

import httpx
from bs4 import BeautifulSoup

if __package__ in {None, ""}:  # Support ``python scripts/crawl_review_records.py``.
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.config import load_sources
from scripts.io_utils import ImmutableSnapshotError, atomic_write_immutable


_MAX_DOWNLOAD_BYTES = 100 * 1024 * 1024
_MAX_ATTEMPTS = 3
_RETRYABLE_STATUS_CODES = frozenset({408, 429, 500, 502, 503, 504})
_SENSITIVE_QUERY_KEY_WORDS = frozenset(
    {"token", "secret", "credential", "signature", "authorization", "password", "apikey", "accesskey"}
)
_ROC_DATE = re.compile(r"^(\d{3})([-./])(\d{1,2})\2(\d{1,2})$")
_DATE_IN_ROW = re.compile(r"\b\d{1,3}[-./]\d{1,2}[-./]\d{1,2}\b")
_UNSAFE_FILENAME = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_REDIRECT_STATUS_CODES = frozenset({301, 302, 303, 307, 308})
_MAX_REDIRECT_HOPS = 5
_MANIFEST_FIELDS = frozenset(
    {"schema_version", "title", "published_date", "detail_url", "attachment_url", "sha256", "byte_length", "retrieved_at"}
)
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_RESERVED_NAMES = frozenset(
    {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}
)
_COMMITTEE_TERMS = ("樹木保護委員會", "樹保委員會", "樹委會")
_REVIEW_ONLY_TERMS = ("幹事會", "專案小組")
_EXCLUDED_TITLE_TERMS = ("會議議程", "議程", "現場會勘", "現勘", "現場勘查")


@dataclass(frozen=True, slots=True)
class MeetingEntry:
    title: str
    detail_url: str
    published_date: date


@dataclass(frozen=True, slots=True)
class DownloadedRecord:
    title: str
    published_date: date
    detail_url: str
    attachment_url: str
    path: Path
    sha256: str
    status: Literal["created", "unchanged", "duplicate"]


def parse_roc_date(value: str) -> date:
    """Convert a strictly formatted Republic-of-China date to Gregorian."""
    matched = _ROC_DATE.fullmatch(value.strip())
    if matched is None:
        raise ValueError("date must use ROC YYY-MM-DD, YYY.MM.DD, or YYY/MM/DD")
    year, _separator, month, day = matched.groups()
    try:
        return date(int(year) + 1911, int(month), int(day))
    except ValueError as error:
        raise ValueError("invalid ROC calendar date") from error


def _validate_official_url(url: str) -> None:
    parsed = urlsplit(url)
    host = parsed.hostname.casefold() if parsed.hostname else ""
    if (
        parsed.scheme.casefold() != "https"
        or not host
        or (host != "gov.taipei" and not host.endswith(".gov.taipei"))
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
    ):
        raise ValueError("URL must use an official Taipei government HTTPS host")
    normalized_keys = (
        re.sub(r"[^a-z0-9]", "", key.casefold())
        for key, _value in parse_qsl(parsed.query, keep_blank_values=True)
    )
    if any(
        key == "key" or any(word in key for word in _SENSITIVE_QUERY_KEY_WORDS)
        for key in normalized_keys
    ):
        raise ValueError("URL must not contain sensitive query parameters")


def _official_url(base_url: str, href: str) -> str | None:
    href = href.strip()
    if not href:
        return None
    url = urljoin(base_url, href)
    try:
        _validate_official_url(url)
    except ValueError:
        return None
    return url


def _next_redirect_url(response: httpx.Response, current_url: str, visited: set[str], hops: int) -> str:
    location = response.headers.get("location")
    if not location:
        raise RuntimeError("official redirect is missing a location")
    redirected_url = urljoin(current_url, location)
    _validate_official_url(redirected_url)
    if redirected_url in visited:
        raise RuntimeError("official redirect cycle detected")
    if hops >= _MAX_REDIRECT_HOPS:
        raise RuntimeError("official redirect hop limit exceeded")
    return redirected_url


def _get_with_retry(client: httpx.Client, url: str) -> httpx.Response:
    last_error: Exception | None = None
    for _attempt in range(_MAX_ATTEMPTS):
        try:
            response = client.get(url, timeout=30.0, follow_redirects=False)
            if response.status_code in _RETRYABLE_STATUS_CODES:
                last_error = RuntimeError("official server returned a temporary failure")
            elif response.is_error:
                raise RuntimeError("official server returned an HTTP error")
            else:
                return response
        except httpx.TransportError as error:
            last_error = error
    raise RuntimeError("official download failed after retries") from last_error


def _stream_pdf_with_retry(client: httpx.Client, url: str) -> tuple[str, bytes | str]:
    last_error: Exception | None = None
    for _attempt in range(_MAX_ATTEMPTS):
        try:
            with client.stream("GET", url, timeout=30.0, follow_redirects=False) as response:
                if response.status_code in _RETRYABLE_STATUS_CODES:
                    last_error = RuntimeError("official server returned a temporary failure")
                elif response.status_code in _REDIRECT_STATUS_CODES:
                    location = response.headers.get("location")
                    if not location:
                        raise RuntimeError("official redirect is missing a location")
                    return "redirect", location
                else:
                    return "pdf", _read_pdf_response(response)
        except httpx.TransportError as error:
            last_error = error
    raise RuntimeError("official download failed after retries") from last_error


def _response_or_retry(client: httpx.Client, url: str, *, stream: bool) -> bytes:
    """Read an official response through validated, manually followed redirects."""
    _validate_official_url(url)
    current_url = url
    visited = {current_url}
    hops = 0
    while True:
        if stream:
            result_kind, result = _stream_pdf_with_retry(client, current_url)
            if result_kind == "pdf":
                assert isinstance(result, bytes)
                return result
            assert isinstance(result, str)
            redirected_url = _next_redirect_url(
                httpx.Response(302, headers={"location": result}), current_url, visited, hops
            )
        else:
            response = _get_with_retry(client, current_url)
            if response.status_code not in _REDIRECT_STATUS_CODES:
                return response.content
            redirected_url = _next_redirect_url(response, current_url, visited, hops)
        hops += 1
        visited.add(redirected_url)
        current_url = redirected_url


def _read_pdf_response(response: httpx.Response) -> bytes:
    if response.is_error:
        raise RuntimeError("official server returned an HTTP error")
    content_length = response.headers.get("content-length")
    if content_length is not None:
        try:
            if int(content_length) > _MAX_DOWNLOAD_BYTES:
                raise ValueError("PDF exceeds 100 MiB limit")
        except ValueError:
            if content_length.strip().isdigit():
                raise
    content_type = response.headers.get("content-type", "").casefold()
    if "html" in content_type:
        raise ValueError("HTML response cannot be stored as a PDF")
    content = bytearray()
    for chunk in response.iter_bytes():
        content.extend(chunk)
        if len(content) > _MAX_DOWNLOAD_BYTES:
            raise ValueError("PDF exceeds 100 MiB limit")
    result = bytes(content)
    if not result.startswith(b"%PDF-"):
        raise ValueError("download does not have PDF magic bytes")
    return result


def _parse_entries(html: bytes, index_url: str, kind: Literal["review", "committee"]) -> list[MeetingEntry]:
    document = BeautifulSoup(html, "html.parser")
    entries: list[MeetingEntry] = []
    seen: set[str] = set()
    for row in document.find_all("tr"):
        text = row.get_text(" ", strip=True)
        date_match = _DATE_IN_ROW.search(text)
        link = row.find("a", href=True)
        if date_match is None or link is None:
            continue
        title = link.get_text(" ", strip=True)
        detail_url = _official_url(index_url, str(link["href"]))
        if not detail_url or not _eligible_title(title, kind):
            continue
        try:
            published_date = parse_roc_date(date_match.group(0))
        except ValueError:
            continue
        unique = f"{detail_url}\n{published_date.isoformat()}"
        if unique not in seen:
            entries.append(MeetingEntry(title, detail_url, published_date))
            seen.add(unique)
    return entries


def _eligible_title(title: str, kind: Literal["review", "committee"]) -> bool:
    normalized = unicodedata.normalize("NFKC", title).replace(" ", "")
    if not any(marker in normalized for marker in ("會議紀錄", "會議記錄")):
        return False
    if any(term in normalized for term in _EXCLUDED_TITLE_TERMS):
        return False
    is_review = any(term in normalized for term in _REVIEW_ONLY_TERMS)
    is_committee = any(term in normalized for term in _COMMITTEE_TERMS)
    return is_review if kind == "review" else is_committee and not is_review


def _pagination_urls(html: bytes, index_url: str) -> list[str]:
    document = BeautifulSoup(html, "html.parser")
    urls: list[str] = []
    for link in document.find_all("a", href=True):
        if not link.get_text(" ", strip=True).isdigit():
            continue
        url = _official_url(index_url, str(link["href"]))
        if url is not None and _is_positive_page_url(url):
            urls.append(url)
    return urls


def _is_positive_page_url(url: str) -> bool:
    values = [value for key, value in parse_qsl(urlsplit(url).query, keep_blank_values=True) if key == "page"]
    return len(values) == 1 and values[0].isdigit() and int(values[0]) > 0


def _attachment_urls(html: bytes, detail_url: str) -> list[str]:
    document = BeautifulSoup(html, "html.parser")
    urls: list[str] = []
    for link in document.find_all("a", href=True):
        href = str(link["href"])
        text = link.get_text(" ", strip=True).casefold()
        link_type = str(link.get("type", "")).casefold()
        path = urlsplit(href).path.casefold()
        if not (path.endswith(".pdf") or "pdf" in text or "pdf" in link_type):
            continue
        url = _official_url(detail_url, href)
        if url is not None and url not in urls:
            urls.append(url)
    return urls


def _safe_title(title: str) -> str:
    value = unicodedata.normalize("NFKC", title)
    value = _UNSAFE_FILENAME.sub("_", value)[:200].rstrip(". ").strip()
    if not value or value in {".", ".."} or value.split(".", 1)[0].upper() in _RESERVED_NAMES:
        raise ValueError("meeting title cannot form a safe file name")
    return value


def _manifest_path(pdf_path: Path) -> Path:
    return pdf_path.with_suffix(".manifest.json")


def _manifest_for(entry: MeetingEntry, attachment_url: str, digest: str, length: int) -> dict[str, object]:
    return {
        "schema_version": 1,
        "title": entry.title,
        "published_date": entry.published_date.isoformat(),
        "detail_url": entry.detail_url,
        "attachment_url": attachment_url,
        "sha256": digest,
        "byte_length": length,
        "retrieved_at": datetime.now(UTC).isoformat(),
    }


def _invalid_manifest(error: Exception | None = None) -> ImmutableSnapshotError:
    return ImmutableSnapshotError("invalid manifest")


def _validate_manifest(manifest: object) -> dict[str, object]:
    if not isinstance(manifest, dict) or set(manifest) != _MANIFEST_FIELDS:
        raise _invalid_manifest()
    if manifest.get("schema_version") != 1 or type(manifest.get("schema_version")) is not int:
        raise _invalid_manifest()
    for field in ("title", "published_date", "detail_url", "attachment_url", "sha256", "retrieved_at"):
        if not isinstance(manifest.get(field), str) or not manifest[field]:
            raise _invalid_manifest()
    if type(manifest.get("byte_length")) is not int or manifest["byte_length"] < 0:
        raise _invalid_manifest()
    try:
        published_date = date.fromisoformat(str(manifest["published_date"]))
        if published_date.isoformat() != manifest["published_date"]:
            raise ValueError
        _validate_official_url(str(manifest["detail_url"]))
        _validate_official_url(str(manifest["attachment_url"]))
        retrieved_at = datetime.fromisoformat(str(manifest["retrieved_at"]))
        if retrieved_at.tzinfo is None or retrieved_at.utcoffset() is None:
            raise ValueError
    except ValueError as error:
        raise _invalid_manifest(error) from error
    if _SHA256.fullmatch(str(manifest["sha256"])) is None:
        raise _invalid_manifest()
    return manifest


def _read_existing_manifest(path: Path) -> dict[str, object]:
    try:
        return _validate_manifest(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, ValueError, TypeError) as error:
        raise _invalid_manifest(error) from error


def _validate_manifest_snapshot(manifest: dict[str, object], pdf_path: Path) -> None:
    try:
        content = pdf_path.read_bytes()
    except OSError as error:
        raise _invalid_manifest(error) from error
    if len(content) != manifest["byte_length"] or hashlib.sha256(content).hexdigest() != manifest["sha256"]:
        raise _invalid_manifest()


def _ensure_manifest(path: Path, manifest: dict[str, object]) -> None:
    _validate_manifest(manifest)
    if path.exists():
        existing = _read_existing_manifest(path)
        if any(existing.get(key) != value for key, value in manifest.items() if key != "retrieved_at"):
            raise ImmutableSnapshotError("existing manifest is inconsistent with snapshot")
        return
    content = (json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
    atomic_write_immutable(path, content)


def _existing_by_hash(out_dir: Path, digest: str) -> Path | None:
    if not out_dir.exists():
        return None
    try:
        manifest_paths = list(out_dir.rglob("*.manifest.json"))
    except OSError as error:
        raise _invalid_manifest(error) from error
    for manifest_path in manifest_paths:
        manifest = _read_existing_manifest(manifest_path)
        candidate = manifest_path.with_suffix("").with_suffix(".pdf")
        _validate_manifest_snapshot(manifest, candidate)
        if manifest["sha256"] == digest:
            return candidate
    return None


def _archive_record(path: Path, payload: bytes, manifest: dict[str, object]) -> Literal["created", "unchanged"]:
    manifest_path = _manifest_path(path)
    if manifest_path.exists():
        existing = _read_existing_manifest(manifest_path)
        if not path.is_file():
            raise _invalid_manifest()
        _validate_manifest_snapshot(existing, path)
    elif path.exists() and not path.is_file():
        raise ImmutableSnapshotError("existing snapshot is invalid")
    status = atomic_write_immutable(path, payload)
    try:
        _ensure_manifest(manifest_path, manifest)
    except Exception as error:
        if status == "created":
            try:
                path.unlink()
            except OSError:
                pass
        raise ImmutableSnapshotError("manifest archival failed") from error
    return status


def crawl_records(
    index_url: str,
    out_dir: Path,
    kind: Literal["review", "committee"],
    client: httpx.Client,
    max_pages: int = 50,
) -> list[DownloadedRecord]:
    """Crawl eligible official meeting records into immutable PDF snapshots."""
    if kind not in {"review", "committee"}:
        raise ValueError("kind must be review or committee")
    if max_pages < 1:
        return []
    _validate_official_url(index_url)
    pending = [index_url]
    visited: set[str] = set()
    entries: list[MeetingEntry] = []
    seen_entries: set[tuple[str, date]] = set()
    while pending and len(visited) < max_pages:
        page_url = pending.pop(0)
        if page_url in visited:
            continue
        html = _response_or_retry(client, page_url, stream=False)
        visited.add(page_url)
        for entry in _parse_entries(html, page_url, kind):
            key = (entry.detail_url, entry.published_date)
            if key not in seen_entries:
                entries.append(entry)
                seen_entries.add(key)
        for pagination_url in _pagination_urls(html, page_url):
            if pagination_url not in visited and pagination_url not in pending:
                pending.append(pagination_url)

    records: list[DownloadedRecord] = []
    attachment_counts: dict[tuple[date, str], int] = {}
    for entry in entries:
        detail = _response_or_retry(client, entry.detail_url, stream=False)
        for attachment_url in _attachment_urls(detail, entry.detail_url):
            payload = _response_or_retry(client, attachment_url, stream=True)
            digest = hashlib.sha256(payload).hexdigest()
            title = _safe_title(entry.title)
            key = (entry.published_date, title)
            attachment_counts[key] = attachment_counts.get(key, 0) + 1
            suffix = "" if attachment_counts[key] == 1 else f"__{attachment_counts[key]}"
            path = out_dir / entry.published_date.strftime("%Y-%m") / f"{title}{suffix}.pdf"
            duplicate_path = _existing_by_hash(out_dir, digest)
            if duplicate_path is not None and duplicate_path != path:
                records.append(
                    DownloadedRecord(entry.title, entry.published_date, entry.detail_url, attachment_url, duplicate_path, digest, "duplicate")
                )
                continue
            status = _archive_record(path, payload, _manifest_for(entry, attachment_url, digest, len(payload)))
            records.append(DownloadedRecord(entry.title, entry.published_date, entry.detail_url, attachment_url, path, digest, status))
    return records


def _write_github_output(records: list[DownloadedRecord]) -> None:
    output_path = os.environ.get("GITHUB_OUTPUT")
    if output_path:
        with Path(output_path).open("a", encoding="utf-8", newline="\n") as output:
            output.write(f"new_files={sum(record.status == 'created' for record in records)}\n")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--kind", required=True, choices=("review", "committee"))
    parser.add_argument("--url")
    parser.add_argument(
        "--config", type=Path, default=Path(__file__).resolve().parents[1] / "config" / "sources.json"
    )
    arguments = parser.parse_args(argv)
    source_name = f"{arguments.kind}_records"
    source = load_sources(arguments.config, os.environ).get(source_name)
    url = arguments.url or (source.url if source is not None else None)
    if not url:
        print(f"{source_name} unavailable; skipping")
        _write_github_output([])
        return 0
    with httpx.Client(timeout=30.0, follow_redirects=False) as client:
        records = crawl_records(url, arguments.out, arguments.kind, client)
    _write_github_output(records)
    print(f"{source_name}: {len(records)} records")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
