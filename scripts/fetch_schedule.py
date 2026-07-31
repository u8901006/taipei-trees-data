"""Safely archive an official Taipei pruning schedule as an immutable snapshot."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Callable, Literal, Mapping, Sequence
from urllib.parse import parse_qsl, urljoin, urlsplit
from zoneinfo import ZoneInfo

import httpx

if __package__ in {None, ""}:  # Support ``python scripts/fetch_schedule.py``.
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.config import load_sources
from scripts.io_utils import ImmutableSnapshotError, atomic_write_immutable


_MAX_DOWNLOAD_BYTES = 100 * 1024 * 1024
_MAX_ATTEMPTS = 3
_MAX_REDIRECT_HOPS = 5
_TRANSIENT_STATUS_CODES = frozenset({408, 429, *range(500, 600)})
_REDIRECT_STATUS_CODES = frozenset({301, 302, 303, 307, 308})
_SECRET_QUERY_WORDS = frozenset(
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
_CONTENT_EXTENSIONS = {
    "text/plain": ".txt",
    "text/csv": ".csv",
    "application/csv": ".csv",
    "application/json": ".json",
    "application/pdf": ".pdf",
}
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_MANIFEST_FIELDS = frozenset(
    {
        "schema_version",
        "source_url",
        "sha256",
        "byte_length",
        "content_type",
        "retrieved_at",
    }
)


class ScheduleFetchError(RuntimeError):
    """A fixed-message remote or content-validation failure."""


@dataclass(frozen=True, slots=True)
class ScheduleResult:
    path: Path
    status: Literal["created", "unchanged"]


def _failure() -> ScheduleFetchError:
    return ScheduleFetchError("schedule fetch failed")


def _validate_official_url(url: str) -> None:
    try:
        parsed = urlsplit(url)
        host = parsed.hostname.casefold() if parsed.hostname else ""
    except (TypeError, ValueError) as error:
        raise _failure() from error
    official_host = (
        host == "data.taipei"
        or host == "gov.taipei"
        or host.endswith(".gov.taipei")
    )
    if (
        parsed.scheme.casefold() != "https"
        or not official_host
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
    ):
        raise _failure()
    normalized_keys = (
        re.sub(r"[^a-z0-9]", "", key.casefold())
        for key, _value in parse_qsl(parsed.query, keep_blank_values=True)
    )
    if any(
        key == "key" or any(secret_word in key for secret_word in _SECRET_QUERY_WORDS)
        for key in normalized_keys
    ):
        raise _failure()


def _normalized_content_type(headers: Mapping[str, str]) -> str:
    content_type = headers.get("content-type", "").split(";", 1)[0].strip().casefold()
    if content_type not in _CONTENT_EXTENSIONS:
        raise _failure()
    return content_type


def _read_bounded_content(response: object) -> bytes:
    headers = getattr(response, "headers", {})
    raw_length = headers.get("content-length")
    if raw_length is not None:
        try:
            declared_length = int(raw_length)
        except (TypeError, ValueError) as error:
            raise _failure() from error
        if declared_length < 0 or declared_length > _MAX_DOWNLOAD_BYTES:
            raise _failure()
    content = bytearray()
    try:
        chunks = response.iter_bytes()
        for chunk in chunks:
            content.extend(chunk)
            if len(content) > _MAX_DOWNLOAD_BYTES:
                raise _failure()
    except ScheduleFetchError:
        raise
    except (OSError, TypeError, ValueError, httpx.TransportError) as error:
        raise _failure() from error
    return bytes(content)


def _reject_duplicate_json(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _decode_text(content: bytes) -> str:
    for encoding in ("utf-8-sig", "utf-8", "cp950", "big5"):
        try:
            return content.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise _failure()


def _validate_content(content_type: str, content: bytes) -> None:
    if not content:
        raise _failure()
    stripped = content.lstrip(b"\xef\xbb\xbf \t\r\n")
    if content_type == "application/pdf":
        if not content.startswith(b"%PDF-"):
            raise _failure()
        return
    if content_type == "application/json":
        try:
            json.loads(content.decode("utf-8-sig"), object_pairs_hook=_reject_duplicate_json)
        except (UnicodeDecodeError, ValueError, TypeError) as error:
            raise _failure() from error
        return
    text = _decode_text(content)
    if "\x00" in text or not text.strip():
        raise _failure()
    lowered = stripped[:64].lower()
    if lowered.startswith((b"<!doctype html", b"<html")):
        raise _failure()


def _request_once(
    client: object,
    url: str,
    sleeper: Callable[[float], None],
) -> tuple[Literal["redirect", "content"], str | tuple[str, bytes]]:
    last_error: Exception | None = None
    for attempt in range(_MAX_ATTEMPTS):
        try:
            with client.stream("GET", url, timeout=30.0, follow_redirects=False) as response:
                status_code = response.status_code
                if status_code in _TRANSIENT_STATUS_CODES:
                    last_error = _failure()
                elif status_code in _REDIRECT_STATUS_CODES:
                    location = response.headers.get("location")
                    if not isinstance(location, str) or not location.strip():
                        raise _failure()
                    return "redirect", location
                elif status_code < 200 or status_code >= 300:
                    raise _failure()
                else:
                    content_type = _normalized_content_type(response.headers)
                    content = _read_bounded_content(response)
                    _validate_content(content_type, content)
                    return "content", (content_type, content)
        except ScheduleFetchError:
            raise
        except httpx.TransportError as error:
            last_error = error
        except (AttributeError, OSError, TypeError, ValueError) as error:
            raise _failure() from error
        if attempt + 1 < _MAX_ATTEMPTS:
            sleeper(0.1 * (attempt + 1))
    raise _failure() from last_error


def _download(
    source_url: str,
    client: object,
    sleeper: Callable[[float], None],
) -> tuple[str, str, bytes]:
    _validate_official_url(source_url)
    current_url = source_url
    visited = {current_url}
    redirect_hops = 0
    while True:
        result_kind, result = _request_once(client, current_url, sleeper)
        if result_kind == "content":
            content_type, content = result
            assert isinstance(content_type, str)
            assert isinstance(content, bytes)
            return current_url, content_type, content
        assert isinstance(result, str)
        redirected_url = urljoin(current_url, result)
        _validate_official_url(redirected_url)
        if redirected_url in visited or redirect_hops >= _MAX_REDIRECT_HOPS:
            raise _failure()
        visited.add(redirected_url)
        redirect_hops += 1
        current_url = redirected_url


def _validate_retrieved_at(value: object) -> None:
    if not isinstance(value, str) or not value:
        raise ImmutableSnapshotError("invalid schedule manifest")
    try:
        timestamp = datetime.fromisoformat(value)
    except ValueError as error:
        raise ImmutableSnapshotError("invalid schedule manifest") from error
    if timestamp.tzinfo is None or timestamp.utcoffset() is None:
        raise ImmutableSnapshotError("invalid schedule manifest")


def _validate_manifest(value: object) -> dict[str, object]:
    if not isinstance(value, dict) or set(value) != _MANIFEST_FIELDS:
        raise ImmutableSnapshotError("invalid schedule manifest")
    if value.get("schema_version") != 1 or type(value.get("schema_version")) is not int:
        raise ImmutableSnapshotError("invalid schedule manifest")
    source_url = value.get("source_url")
    if not isinstance(source_url, str):
        raise ImmutableSnapshotError("invalid schedule manifest")
    try:
        _validate_official_url(source_url)
    except ScheduleFetchError as error:
        raise ImmutableSnapshotError("invalid schedule manifest") from error
    digest = value.get("sha256")
    if not isinstance(digest, str) or _SHA256.fullmatch(digest) is None:
        raise ImmutableSnapshotError("invalid schedule manifest")
    byte_length = value.get("byte_length")
    if type(byte_length) is not int or byte_length < 0:
        raise ImmutableSnapshotError("invalid schedule manifest")
    content_type = value.get("content_type")
    if not isinstance(content_type, str) or content_type not in _CONTENT_EXTENSIONS:
        raise ImmutableSnapshotError("invalid schedule manifest")
    _validate_retrieved_at(value.get("retrieved_at"))
    return value


def _read_manifest(path: Path) -> dict[str, object]:
    try:
        value = json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=_reject_duplicate_json,
        )
    except (OSError, UnicodeError, ValueError, TypeError) as error:
        raise ImmutableSnapshotError("invalid schedule manifest") from error
    return _validate_manifest(value)


def _manifest_path(snapshot_path: Path) -> Path:
    return snapshot_path.with_name(f"{snapshot_path.name}.manifest.json")


def _manifest_payload(
    source_url: str,
    content_type: str,
    content: bytes,
    retrieved_at: datetime,
) -> dict[str, object]:
    return {
        "schema_version": 1,
        "source_url": source_url,
        "sha256": hashlib.sha256(content).hexdigest(),
        "byte_length": len(content),
        "content_type": content_type,
        "retrieved_at": retrieved_at.isoformat(),
    }


def _validate_existing_pair(
    snapshot_path: Path,
    manifest_path: Path,
    expected: dict[str, object],
    content: bytes,
) -> Literal["unchanged"]:
    if snapshot_path.exists() != manifest_path.exists():
        raise ImmutableSnapshotError("schedule snapshot pair is incomplete")
    if not snapshot_path.exists():
        raise ImmutableSnapshotError("schedule snapshot pair is missing")
    if not snapshot_path.is_file() or not manifest_path.is_file():
        raise ImmutableSnapshotError("schedule snapshot pair is invalid")
    manifest = _read_manifest(manifest_path)
    try:
        existing_content = snapshot_path.read_bytes()
    except OSError as error:
        raise ImmutableSnapshotError("schedule snapshot is unreadable") from error
    if (
        len(existing_content) != manifest["byte_length"]
        or hashlib.sha256(existing_content).hexdigest() != manifest["sha256"]
    ):
        raise ImmutableSnapshotError("schedule snapshot does not match manifest")
    for field in ("source_url", "sha256", "byte_length", "content_type"):
        if manifest[field] != expected[field]:
            raise ImmutableSnapshotError("immutable schedule snapshot conflicts")
    if existing_content != content:
        raise ImmutableSnapshotError("immutable schedule snapshot conflicts")
    return "unchanged"


def _archive(
    out_dir: Path,
    source_url: str,
    content_type: str,
    content: bytes,
    retrieved_at: datetime,
) -> ScheduleResult:
    taipei_date = retrieved_at.astimezone(ZoneInfo("Asia/Taipei")).date().isoformat()
    extension = _CONTENT_EXTENSIONS[content_type]
    day_dir = out_dir / taipei_date
    snapshot_path = day_dir / f"pruning_schedule{extension}"
    manifest_path = _manifest_path(snapshot_path)
    expected = _manifest_payload(source_url, content_type, content, retrieved_at)

    if day_dir.exists():
        try:
            existing = {
                path
                for path in day_dir.iterdir()
                if path.name.startswith("pruning_schedule")
            }
        except OSError as error:
            raise ImmutableSnapshotError("schedule archive is unreadable") from error
        if existing - {snapshot_path, manifest_path}:
            raise ImmutableSnapshotError("immutable schedule content type conflicts")
        if snapshot_path.exists() or manifest_path.exists():
            status = _validate_existing_pair(
                snapshot_path,
                manifest_path,
                expected,
                content,
            )
            return ScheduleResult(snapshot_path, status)

    manifest_bytes = (
        json.dumps(expected, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    ).encode("utf-8")
    snapshot_status = atomic_write_immutable(snapshot_path, content)
    try:
        atomic_write_immutable(manifest_path, manifest_bytes)
    except Exception:
        if snapshot_status == "created":
            snapshot_path.unlink(missing_ok=True)
        raise
    return ScheduleResult(snapshot_path, snapshot_status)


def fetch_schedule(
    source_url: str,
    out_dir: Path,
    client: object,
    *,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    sleeper: Callable[[float], None] = time.sleep,
) -> ScheduleResult:
    """Download and archive one official pruning schedule."""
    retrieved_at = clock()
    if retrieved_at.tzinfo is None or retrieved_at.utcoffset() is None:
        raise ScheduleFetchError("schedule fetch failed")
    final_url, content_type, content = _download(source_url, client, sleeper)
    return _archive(out_dir, final_url, content_type, content, retrieved_at)


def _write_github_output(path_text: str | None, status: str, new_files: int) -> None:
    if not path_text:
        return
    with Path(path_text).open("a", encoding="utf-8", newline="\n") as output:
        output.write(f"status={status}\n")
        output.write(f"new_files={new_files}\n")


def main(
    argv: Sequence[str] | None = None,
    *,
    environ: Mapping[str, str] | None = None,
    client_factory: Callable[[], object] | None = None,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    sleeper: Callable[[float], None] = time.sleep,
) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument(
        "--config",
        type=Path,
        default=Path(__file__).resolve().parents[1] / "config" / "sources.json",
    )
    arguments = parser.parse_args(argv)
    effective_environ = os.environ if environ is None else environ
    try:
        sources = load_sources(arguments.config, effective_environ)
        source = sources.get("pruning_schedule")
        if source is None or source.url is None:
            print("修剪時程來源尚未設定。")
            _write_github_output(
                effective_environ.get("GITHUB_OUTPUT"),
                "not_configured",
                0,
            )
            return 0
        factory = client_factory or (
            lambda: httpx.Client(timeout=30.0, follow_redirects=False)
        )
        with factory() as client:
            result = fetch_schedule(
                source.url,
                arguments.out,
                client,
                clock=clock,
                sleeper=sleeper,
            )
        _write_github_output(
            effective_environ.get("GITHUB_OUTPUT"),
            "available",
            int(result.status == "created"),
        )
        print("修剪時程資料已取得。")
        return 0
    except (
        ImmutableSnapshotError,
        ScheduleFetchError,
        OSError,
        TypeError,
        ValueError,
    ):
        print("修剪時程擷取失敗。", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
