"""Fetch immutable, deterministic raw snapshots from Taipei open-data sources."""

from __future__ import annotations

import argparse
import gzip
import hashlib
import io
import json
import os
import re
import sys
import time
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Callable, Literal, Sequence
from urllib.parse import parse_qsl, urlsplit

import httpx

if __package__ in {None, ""}:  # Support ``python scripts/fetch_opendata.py``.
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.config import SourceConfig, load_sources
from scripts.io_utils import ImmutableSnapshotError, atomic_write_immutable
from scripts.taipei_api import (
    REDIRECT_STATUS_CODES,
    Resource,
    resolve_dataset_resources,
    resolve_official_redirect,
    validate_official_https_url,
)


_MAX_DOWNLOAD_BYTES = 100 * 1024 * 1024
_MAX_ATTEMPTS = 3
_RETRYABLE_STATUS_CODES = frozenset({408, 429, 500, 502, 503, 504})
_UNSAFE_COMPONENT = re.compile(r"[^A-Za-z0-9_-]+")
_SENSITIVE_QUERY_KEY_WORDS = frozenset(
    {
        "token",
        "secret",
        "credential",
        "signature",
        "authorization",
        "password",
        "apikey",
        "accesskey",
    }
)


@dataclass(frozen=True, slots=True)
class FetchResult:
    source_name: str
    path: Path | None
    checksum: str | None
    status: Literal["created", "unchanged", "skipped"]

    @property
    def changed(self) -> bool:
        return self.status == "created"

    @property
    def skipped(self) -> bool:
        return self.status == "skipped"


def _safe_component(value: str) -> str:
    sanitized = _UNSAFE_COMPONENT.sub("_", value).strip("._")
    if not sanitized or sanitized in {".", ".."}:
        raise ValueError("source name cannot form a safe path component")
    return sanitized


def _validate_download_url(url: str) -> None:
    validate_official_https_url(url)
    parsed = urlsplit(url)
    if "@" in parsed.netloc:
        raise ValueError("download URL must not contain user credentials")
    if parsed.fragment:
        raise ValueError("download URL must not contain a fragment")
    normalized_keys = (
        re.sub(r"[^a-z0-9]", "", key.casefold())
        for key, _ in parse_qsl(parsed.query, keep_blank_values=True)
    )
    if any(
        key == "key" or any(sensitive_word in key for sensitive_word in _SENSITIVE_QUERY_KEY_WORDS)
        for key in normalized_keys
    ):
        raise ValueError("download URL must not contain sensitive query parameters")


def _select_street_tree_resource(resources: list[Resource]) -> Resource:
    candidates: list[Resource] = []
    for resource in resources:
        name = resource.name.casefold()
        if any(
            excluded in name
            for excluded in ("公園", "公园", "park", "樹穴", "树穴", "tree hole", "tree-hole")
        ):
            continue
        if (
            "行道" in name
            or "street tree" in name
            or "street_tree" in name
            or "street-tree" in name
        ):
            candidates.append(resource)
    if len(candidates) != 1:
        raise RuntimeError("could not select exactly one street-tree CSV resource")
    return candidates[0]


def _select_resource(source: SourceConfig, resources: list[Resource]) -> Resource:
    if source.name == "street_trees":
        return _select_street_tree_resource(resources)
    if source.name == "protected_trees":
        protected = [
            resource
            for resource in resources
            if any(
                term in resource.name.casefold() for term in ("保護樹", "保护树", "protected tree")
            )
        ]
        if len(protected) == 1:
            return protected[0]
    if len(resources) == 1:
        return resources[0]
    raise RuntimeError(f"could not select exactly one CSV resource for {source.name}")


def _download_csv(client: httpx.Client, url: str) -> bytes:
    _validate_download_url(url)
    last_error: Exception | None = None
    for attempt in range(_MAX_ATTEMPTS):
        current_url = url
        visited = {url}
        redirect_hops = 0
        try:
            while True:
                with client.stream(
                    "GET",
                    current_url,
                    timeout=30.0,
                    follow_redirects=False,
                ) as response:
                    if response.status_code in REDIRECT_STATUS_CODES:
                        current_url = resolve_official_redirect(
                            response,
                            current_url,
                            visited,
                            redirect_hops,
                        )
                        _validate_download_url(current_url)
                        redirect_hops += 1
                        continue
                    if response.status_code in _RETRYABLE_STATUS_CODES:
                        last_error = RuntimeError(
                            f"download failed with HTTP {response.status_code}"
                        )
                        break
                    if response.is_error:
                        raise RuntimeError(f"download failed with HTTP {response.status_code}")
                    content_length = response.headers.get("content-length")
                    if content_length is not None and int(content_length) > _MAX_DOWNLOAD_BYTES:
                        raise ValueError("download exceeds 100 MiB limit")
                    content_type = response.headers.get("content-type", "").casefold()
                    if "html" in content_type:
                        raise ValueError("HTML response cannot be used as CSV")
                    chunks = bytearray()
                    for chunk in response.iter_bytes():
                        chunks.extend(chunk)
                        if len(chunks) > _MAX_DOWNLOAD_BYTES:
                            raise ValueError("download exceeds 100 MiB limit")
                    content = bytes(chunks)
                    if (
                        content.lstrip(b"\xef\xbb\xbf \t\r\n")
                        .lower()
                        .startswith((b"<!doctype html", b"<html"))
                    ):
                        raise ValueError("HTML response cannot be used as CSV")
                    return content
        except httpx.TransportError as error:
            last_error = error
        if attempt + 1 < _MAX_ATTEMPTS:
            time.sleep(0.1 * (attempt + 1))
    raise RuntimeError("download failed after retries") from last_error


def _deterministic_gzip(content: bytes) -> bytes:
    buffer = io.BytesIO()
    with gzip.GzipFile(fileobj=buffer, mode="wb", filename="", mtime=0) as archive:
        archive.write(content)
    return buffer.getvalue()


def _manifest_path(snapshot_path: Path) -> Path:
    return snapshot_path.with_suffix("").with_suffix(".json")


def _ensure_manifest(manifest_path: Path, manifest: dict[str, object]) -> None:
    if manifest_path.exists():
        try:
            existing_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as error:
            raise ImmutableSnapshotError("existing manifest is invalid") from error
        if not isinstance(existing_manifest, dict) or any(
            existing_manifest.get(field) != value
            for field, value in manifest.items()
            if field != "retrieved_at"
        ):
            raise ImmutableSnapshotError("existing manifest is inconsistent with snapshot")
        return
    content = (json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode(
        "utf-8"
    )
    atomic_write_immutable(manifest_path, content)


def _write_snapshot(snapshot_path: Path, content: bytes) -> Literal["created", "unchanged"]:
    compressed = _deterministic_gzip(content)
    try:
        return atomic_write_immutable(snapshot_path, compressed)
    except ImmutableSnapshotError:
        try:
            existing_content = gzip.decompress(snapshot_path.read_bytes())
        except OSError:
            raise
        if existing_content == content:
            return "unchanged"
        raise


def fetch_dataset(
    source: SourceConfig,
    out_dir: Path,
    snapshot_date: date,
    client: httpx.Client,
    *,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> FetchResult:
    """Fetch one source into its append-only daily raw snapshot."""
    if not source.available:
        if source.required:
            raise RuntimeError(f"required source is unavailable: {source.name}")
        return FetchResult(source.name, None, None, "skipped")
    retrieved_at = clock()
    if retrieved_at.tzinfo is None or retrieved_at.utcoffset() is None:
        raise ValueError("snapshot clock must be timezone-aware")

    resource: Resource | None = None
    if source.url is not None:
        download_url = source.url
    else:
        assert source.dataset_id is not None
        resource = _select_resource(source, resolve_dataset_resources(source.dataset_id, client))
        download_url = resource.download_url

    _validate_download_url(download_url)
    content = _download_csv(client, download_url)
    checksum = hashlib.sha256(content).hexdigest()
    safe_source_name = _safe_component(source.name)
    snapshot_path = out_dir / safe_source_name / f"{snapshot_date.isoformat()}.csv.gz"
    status = _write_snapshot(snapshot_path, content)
    manifest = {
        "source_name": source.name,
        "dataset_id": source.dataset_id,
        "resource_id": resource.identifier if resource is not None else None,
        "original_url": download_url,
        "retrieved_at": retrieved_at.astimezone(UTC).isoformat(),
        "uncompressed_byte_length": len(content),
        "sha256": checksum,
    }
    _ensure_manifest(_manifest_path(snapshot_path), manifest)
    return FetchResult(source.name, snapshot_path, checksum, status)


def _parse_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("date must use YYYY-MM-DD") from error


def _write_github_output(results: list[FetchResult]) -> None:
    output_path = os.environ.get("GITHUB_OUTPUT")
    if output_path is None:
        return
    changed = any(result.changed for result in results)
    fetched_count = sum(not result.skipped for result in results)
    skipped_sources = ",".join(result.source_name for result in results if result.skipped)
    with Path(output_path).open("a", encoding="utf-8", newline="\n") as output:
        output.write(f"changed={str(changed).lower()}\n")
        output.write(f"fetched_count={fetched_count}\n")
        output.write(f"skipped_sources={skipped_sources}\n")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--date", type=_parse_date, default=date.today())
    parser.add_argument(
        "--config",
        type=Path,
        default=Path(__file__).resolve().parents[1] / "config" / "sources.json",
    )
    arguments = parser.parse_args(argv)
    sources = load_sources(arguments.config, os.environ)
    results: list[FetchResult] = []
    with httpx.Client(timeout=30.0, follow_redirects=False) as client:
        for source_name in ("street_trees", "park_trees", "protected_trees"):
            source = sources.get(source_name)
            if source is None:
                continue
            result = fetch_dataset(source, arguments.out, arguments.date, client)
            results.append(result)
            print(f"{source.name}: {result.status}")
    _write_github_output(results)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
