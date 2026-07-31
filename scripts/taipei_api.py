"""Small, isolated client for the official data.taipei dataset metadata API."""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote, urljoin, urlsplit

import httpx


_RETRYABLE_STATUS_CODES = frozenset({408, 429, 500, 502, 503, 504})
_MAX_ATTEMPTS = 3
REDIRECT_STATUS_CODES = frozenset({301, 302, 303, 307, 308})
_MAX_REDIRECT_HOPS = 5
_OFFICIAL_HOSTS = frozenset({"data.taipei"})


def validate_official_https_url(url: str) -> None:
    """Accept only credential-free HTTPS URLs on an explicit official host."""
    parsed = urlsplit(url)
    try:
        port = parsed.port
    except ValueError as error:
        raise ValueError("URL must use an official HTTPS endpoint") from error
    if (
        parsed.scheme.casefold() != "https"
        or parsed.hostname is None
        or parsed.hostname.casefold() not in _OFFICIAL_HOSTS
        or port not in {None, 443}
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
    ):
        raise ValueError("URL must use an official HTTPS endpoint")


def resolve_official_redirect(
    response: httpx.Response,
    current_url: str,
    visited: set[str],
    redirect_hops: int,
) -> str:
    """Resolve one redirect only after validating its complete target."""
    if redirect_hops >= _MAX_REDIRECT_HOPS:
        raise RuntimeError("official redirect limit exceeded")
    location = response.headers.get("location")
    if not location:
        raise RuntimeError("official redirect is missing a location")
    target = urljoin(current_url, location)
    validate_official_https_url(target)
    if target in visited:
        raise RuntimeError("official redirect cycle detected")
    visited.add(target)
    return target


@dataclass(frozen=True, slots=True)
class Resource:
    """One downloadable official dataset resource."""

    identifier: str
    name: str
    format: str
    download_url: str


def _get_with_retries(client: httpx.Client, url: str) -> httpx.Response:
    """Request metadata with the bounded retry policy shared by this fetch layer."""
    validate_official_https_url(url)
    last_error: Exception | None = None
    for attempt in range(_MAX_ATTEMPTS):
        current_url = url
        visited = {url}
        redirect_hops = 0
        try:
            while True:
                response = client.get(current_url, timeout=30.0, follow_redirects=False)
                if response.status_code in REDIRECT_STATUS_CODES:
                    current_url = resolve_official_redirect(
                        response,
                        current_url,
                        visited,
                        redirect_hops,
                    )
                    redirect_hops += 1
                    continue
                break
        except httpx.TransportError as error:
            last_error = error
        else:
            if response.status_code not in _RETRYABLE_STATUS_CODES:
                if response.is_error:
                    raise RuntimeError(
                        f"dataset metadata request failed with HTTP {response.status_code}"
                    )
                return response
            last_error = RuntimeError(
                f"dataset metadata request failed with HTTP {response.status_code}"
            )
        if attempt + 1 < _MAX_ATTEMPTS:
            time.sleep(0.1 * (attempt + 1))
    raise RuntimeError("dataset metadata request failed after retries") from last_error


def _parse_resources(payload: Any) -> list[Resource]:
    if not isinstance(payload, dict):
        raise ValueError("dataset metadata must be a JSON object")
    result = payload.get("result")
    if not isinstance(result, dict):
        raise ValueError("dataset metadata has no result object")
    raw_resources = result.get("resources")
    if not isinstance(raw_resources, list):
        raise ValueError("dataset metadata has no resources list")

    resources: list[Resource] = []
    for raw_resource in raw_resources:
        if not isinstance(raw_resource, dict):
            continue
        identifier = raw_resource.get("id")
        name = raw_resource.get("name")
        resource_format = raw_resource.get("format")
        download_url = raw_resource.get("url")
        if not all(
            isinstance(value, str) and value.strip()
            for value in (identifier, name, resource_format, download_url)
        ):
            continue
        if resource_format.casefold() != "csv":
            continue
        resources.append(
            Resource(
                identifier=identifier,
                name=name,
                format=resource_format,
                download_url=download_url,
            )
        )
    return resources


def resolve_dataset_resources(dataset_id: str, client: httpx.Client) -> list[Resource]:
    """Return downloadable CSV resources from an official data.taipei dataset."""
    if not dataset_id.strip():
        raise ValueError("dataset_id must not be blank")
    endpoint = f"https://data.taipei/api/v1/dataset/{quote(dataset_id, safe='')}"
    response = _get_with_retries(client, endpoint)
    try:
        payload = response.json()
    except ValueError as error:
        raise ValueError("dataset metadata is not valid JSON") from error
    return _parse_resources(payload)
