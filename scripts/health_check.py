"""Produce a safe, deterministic health report for configured public sources."""

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
from typing import Any, Literal
from urllib.parse import parse_qsl, quote, urljoin, urlsplit

import httpx

if __package__ in {None, ""}:  # Support ``python scripts/health_check.py``.
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.config import SourceConfig, load_sources


SCHEMA_VERSION = "1.0"
MAX_ATTEMPTS = 3
MAX_REDIRECT_HOPS = 5
TIMEOUT_SECONDS = 15.0
RETRYABLE_STATUSES = frozenset({408, 429})
REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
STATUSES = frozenset({"available", "unavailable", "not_configured"})
UNAVAILABLE_REASONS = frozenset({"probe_failed", "redirect_rejected"})
SENSITIVE_QUERY_WORDS = frozenset(
    {"token", "secret", "credential", "signature", "authorization", "password", "apikey", "accesskey"}
)
SOURCE_KINDS: dict[str, Literal["dataset", "url"]] = {
    "street_trees": "dataset",
    "protected_trees": "dataset",
    "pruning_schedule": "url",
    "review_records": "url",
    "committee_records": "url",
}
URL_MEDIA_TYPES = frozenset(
    {
        "text/html",
        "text/plain",
        "text/csv",
        "application/csv",
        "application/pdf",
        "application/xhtml+xml",
    }
)
CLI_ERROR_MESSAGE = "健康報告設定、歷史資料或寫入失敗。"


class HealthHistoryError(ValueError):
    """Raised for malformed continuity history without exposing its contents."""


class HealthConfigurationError(ValueError):
    """Raised for unsafe source configuration without exposing its URL."""


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    document: dict[str, object] = {}
    for key, value in pairs:
        if key in document:
            raise ValueError("duplicate JSON key")
        document[key] = value
    return document


def _decode_strict_json(
    text: str,
    error_type: type[ValueError],
    message: str,
) -> object:
    try:
        return json.loads(text, object_pairs_hook=_reject_duplicate_keys)
    except (TypeError, ValueError) as error:
        raise error_type(message) from error


def _aware_timestamp(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None and parsed.utcoffset() is not None else None


def _is_official_taipei_host(hostname: str | None) -> bool:
    if hostname is None:
        return False
    host = hostname.casefold().rstrip(".")
    return host == "data.taipei" or host.endswith(".gov.taipei") or host.endswith(".taipei.gov.tw")


def _validate_safe_url(url: str) -> None:
    parsed = urlsplit(url)
    if parsed.scheme != "https" or not _is_official_taipei_host(parsed.hostname):
        raise HealthConfigurationError("source URL is unsafe")
    if parsed.username is not None or parsed.password is not None or parsed.fragment:
        raise HealthConfigurationError("source URL is unsafe")
    for key, _value in parse_qsl(parsed.query, keep_blank_values=True):
        normalized = "".join(character for character in key.casefold() if character.isalnum())
        if normalized == "key" or any(word in normalized for word in SENSITIVE_QUERY_WORDS):
            raise HealthConfigurationError("source URL is unsafe")


def _validate_history(report: object) -> dict[str, dict[str, object]]:
    if not isinstance(report, dict) or set(report) != {"schema_version", "generated_at", "sources"}:
        raise HealthHistoryError("health history is invalid")
    if report["schema_version"] != SCHEMA_VERSION or _aware_timestamp(report["generated_at"]) is None:
        raise HealthHistoryError("health history is invalid")
    sources = report["sources"]
    if not isinstance(sources, list):
        raise HealthHistoryError("health history is invalid")

    expected = {"name", "kind", "required", "status", "checked_at", "reason", "unavailable_since"}
    previous: dict[str, dict[str, object]] = {}
    for entry in sources:
        if not isinstance(entry, dict) or set(entry) != expected:
            raise HealthHistoryError("health history is invalid")
        name = entry.get("name")
        kind = entry.get("kind")
        required = entry.get("required")
        status = entry.get("status")
        checked_at = _aware_timestamp(entry.get("checked_at"))
        reason = entry.get("reason")
        unavailable_since = entry.get("unavailable_since")
        invalid_common_contract = (
            not isinstance(name, str)
            or not name
            or kind not in {"dataset", "url"}
            or not isinstance(required, bool)
            or status not in STATUSES
            or checked_at is None
            or (unavailable_since is not None and _aware_timestamp(unavailable_since) is None)
            or name in previous
        )
        if invalid_common_contract:
            raise HealthHistoryError("health history is invalid")
        if status == "available" and (reason is not None or unavailable_since is not None):
            raise HealthHistoryError("health history is invalid")
        if status == "unavailable" and (
            reason not in UNAVAILABLE_REASONS or unavailable_since is None
        ):
            raise HealthHistoryError("health history is invalid")
        if status == "not_configured" and (
            reason != "source_not_configured" or unavailable_since is not None
        ):
            raise HealthHistoryError("health history is invalid")
        previous[name] = entry
    return previous


def _is_retryable_status(status_code: object) -> bool:
    return (
        type(status_code) is int
        and (status_code in RETRYABLE_STATUSES or 500 <= status_code < 600)
    )


def _get_with_retries(
    client: Any,
    url: str,
    sleeper: Callable[[float], None],
) -> Any | None:
    for attempt in range(MAX_ATTEMPTS):
        try:
            response = client.get(url, timeout=TIMEOUT_SECONDS, follow_redirects=False)
        except httpx.TransportError:
            response = None
        if response is not None and not _is_retryable_status(response.status_code):
            return response
        if attempt + 1 < MAX_ATTEMPTS:
            sleeper(0.1 * (attempt + 1))
    return None


def _media_type_is_allowed(content_type: object, kind: Literal["dataset", "url"]) -> bool:
    if not isinstance(content_type, str):
        return False
    media_type = content_type.split(";", 1)[0].strip().casefold()
    is_json = media_type == "application/json" or (
        media_type.startswith("application/") and media_type.endswith("+json")
    )
    if kind == "dataset":
        return is_json
    return is_json or media_type in URL_MEDIA_TYPES


def _probe(
    client: Any,
    url: str,
    sleeper: Callable[[float], None],
    *,
    kind: Literal["dataset", "url"],
) -> tuple[bool, str | None]:
    """Perform a bounded probe without exposing a response body or URL details."""
    current_url = url
    visited = {current_url}
    redirect_hops = 0
    while True:
        response = _get_with_retries(client, current_url, sleeper)
        if response is None:
            return False, "probe_failed"
        status_code = response.status_code
        if type(status_code) is not int:
            return False, "probe_failed"
        if status_code in REDIRECT_STATUSES:
            location = response.headers.get("location")
            if not isinstance(location, str) or not location:
                return False, "redirect_rejected"
            redirected_url = urljoin(current_url, location)
            try:
                _validate_safe_url(redirected_url)
            except HealthConfigurationError:
                return False, "redirect_rejected"
            if redirect_hops >= MAX_REDIRECT_HOPS or redirected_url in visited:
                return False, "redirect_rejected"
            visited.add(redirected_url)
            current_url = redirected_url
            redirect_hops += 1
            continue
        if not 200 <= status_code < 300:
            return False, "probe_failed"
        content_type = response.headers.get("content-type")
        if not _media_type_is_allowed(content_type, kind):
            return False, "probe_failed"
        return True, None


def _source_kind(source: SourceConfig) -> Literal["dataset", "url"]:
    """Resolve kind without guessing when an unknown source has no locator."""
    if source.dataset_id is not None and source.url is not None:
        raise HealthConfigurationError("source configuration is invalid")
    locator_kind: Literal["dataset", "url"] | None = None
    if source.dataset_id is not None:
        locator_kind = "dataset"
    elif source.url is not None:
        locator_kind = "url"
    declared_kind = SOURCE_KINDS.get(source.name)
    if declared_kind is not None and locator_kind not in {None, declared_kind}:
        raise HealthConfigurationError("source configuration is invalid")
    if declared_kind is not None:
        return declared_kind
    if locator_kind is not None:
        return locator_kind
    raise HealthConfigurationError("source configuration is invalid")


def _source_probe_url(source: SourceConfig) -> tuple[Literal["dataset", "url"], str]:
    kind = _source_kind(source)
    if kind == "dataset":
        assert source.dataset_id is not None
        return "dataset", f"https://data.taipei/api/v1/dataset/{quote(source.dataset_id, safe='')}"
    assert source.url is not None
    _validate_safe_url(source.url)
    return "url", source.url


def build_health_report(
    sources: Mapping[str, SourceConfig],
    client: Any,
    *,
    previous_report: object | None,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    sleeper: Callable[[float], None] = time.sleep,
) -> dict[str, object]:
    """Return the exact, safe report document for all configured sources."""
    previous = {} if previous_report is None else _validate_history(previous_report)
    now = clock()
    if now.tzinfo is None or now.utcoffset() is None:
        raise HealthConfigurationError("health clock must be timezone-aware")
    checked_at = now.astimezone(UTC).isoformat()
    entries: list[dict[str, object]] = []
    for name in sorted(sources):
        source = sources[name]
        if source.name != name:
            raise HealthConfigurationError("source configuration is invalid")
        kind = _source_kind(source)
        if not source.available:
            entries.append(
                {
                    "name": name,
                    "kind": kind,
                    "required": source.required,
                    "status": "not_configured",
                    "checked_at": checked_at,
                    "reason": "source_not_configured",
                    "unavailable_since": None,
                }
            )
            continue
        kind, url = _source_probe_url(source)
        available, failure_reason = _probe(client, url, sleeper, kind=kind)
        prior = previous.get(name)
        if available:
            status: Literal["available", "unavailable"] = "available"
            unavailable_since: str | None = None
            reason: str | None = None
        else:
            status = "unavailable"
            prior_since = (
                prior.get("unavailable_since")
                if prior
                and prior.get("kind") == kind
                and prior.get("status") == "unavailable"
                else None
            )
            unavailable_since = prior_since if isinstance(prior_since, str) else checked_at
            reason = failure_reason
        entries.append(
            {
                "name": name,
                "kind": kind,
                "required": source.required,
                "status": status,
                "checked_at": checked_at,
                "reason": reason,
                "unavailable_since": unavailable_since,
            }
        )
    return {"schema_version": SCHEMA_VERSION, "generated_at": checked_at, "sources": entries}


def _read_history(path: Path) -> object | None:
    if not path.exists():
        return None
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as error:
        raise HealthHistoryError("health history is invalid") from error
    return _decode_strict_json(text, HealthHistoryError, "health history is invalid")


def _validate_config_json(path: Path) -> None:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as error:
        raise HealthConfigurationError("source configuration is invalid") from error
    _decode_strict_json(text, HealthConfigurationError, "source configuration is invalid")


def _write_report(path: Path, document: Mapping[str, object]) -> None:
    content = (json.dumps(document, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile("wb", dir=path.parent, delete=False) as handle:
            temporary = Path(handle.name)
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def main(
    argv: Sequence[str] | None = None,
    *,
    environ: Mapping[str, str] | None = None,
    client_factory: Callable[[], Any] | None = None,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--config", type=Path, default=Path(__file__).resolve().parents[1] / "config" / "sources.json")
    arguments = parser.parse_args(argv)
    try:
        _validate_config_json(arguments.config)
        sources = load_sources(arguments.config, os.environ if environ is None else environ)
        history = _read_history(arguments.out)
        factory = client_factory or (lambda: httpx.Client())
        with factory() as client:
            document = build_health_report(sources, client, previous_report=history, clock=clock)
        _write_report(arguments.out, document)
    except (OSError, TypeError, ValueError):
        print(CLI_ERROR_MESSAGE, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
