from __future__ import annotations

from datetime import UTC, datetime

import pytest

from scripts.fetch_species_images import (
    choose_species,
    compact_commons_result,
    refresh_species_images,
)


NOW = datetime(2026, 8, 2, 12, 0, tzinfo=UTC)


def _commons_payload() -> dict[str, object]:
    return {
        "query": {
            "pages": {
                "42": {
                    "index": 1,
                    "title": "File:Ficus microcarpa tree.jpg",
                    "imageinfo": [
                        {
                            "url": "https://upload.wikimedia.org/tree-original.jpg",
                            "thumburl": "https://upload.wikimedia.org/tree-900px.jpg",
                            "descriptionurl": (
                                "https://commons.wikimedia.org/wiki/File:Ficus_microcarpa_tree.jpg"
                            ),
                            "mime": "image/jpeg",
                            "mediatype": "BITMAP",
                            "extmetadata": {
                                "LicenseShortName": {"value": "CC BY-SA 4.0"},
                                "Artist": {"value": '<a href="/wiki/User:Leaf">Leaf</a>'},
                                "Credit": {"value": "Own work"},
                            },
                        }
                    ],
                }
            }
        }
    }


def test_compact_commons_result_keeps_safe_photo_and_plain_attribution() -> None:
    result = compact_commons_result("榕", "Ficus microcarpa", _commons_payload(), NOW)

    assert result == {
        "species": "榕",
        "status": "available",
        "query": "Ficus microcarpa",
        "image_url": "https://upload.wikimedia.org/tree-900px.jpg",
        "source_page_url": ("https://commons.wikimedia.org/wiki/File:Ficus_microcarpa_tree.jpg"),
        "license": "CC BY-SA 4.0",
        "artist": "Leaf",
        "credit": "Own work",
        "retrieved_at": "2026-08-02T12:00:00+00:00",
    }


@pytest.mark.parametrize(
    "mutator",
    [
        lambda payload: payload["query"]["pages"]["42"]["imageinfo"][0].update(
            {"thumburl": "javascript:alert(1)"}
        ),
        lambda payload: payload["query"]["pages"]["42"]["imageinfo"][0].update(
            {"descriptionurl": "https://example.com/not-commons"}
        ),
        lambda payload: payload["query"]["pages"]["42"]["imageinfo"][0].update(
            {"mime": "image/svg+xml", "mediatype": "DRAWING"}
        ),
    ],
)
def test_compact_commons_result_rejects_unsafe_or_non_photo_results(mutator) -> None:
    payload = _commons_payload()
    mutator(payload)

    assert compact_commons_result("榕", "Ficus microcarpa", payload, NOW) is None


def test_compact_commons_result_accepts_exact_chinese_wikipedia_fallback() -> None:
    payload = {
        "wikipedia": {
            "query": {
                "pages": [
                    {
                        "title": "串錢柳",
                        "fullurl": "https://zh.wikipedia.org/wiki/%E4%B8%B2%E9%8C%A2%E6%9F%B3",
                        "thumbnail": {"source": "https://upload.wikimedia.org/tree-900px.jpg"},
                    }
                ]
            }
        }
    }

    result = compact_commons_result("串錢柳", "串錢柳", payload, NOW)

    assert result is not None
    assert result["image_url"] == "https://upload.wikimedia.org/tree-900px.jpg"
    assert result["source_page_url"].startswith("https://zh.wikipedia.org/wiki/")
    assert result["license"] is None


def test_choose_species_prioritizes_missing_then_oldest_available() -> None:
    profiles = [
        {"species": "榕", "scientific_name": "Ficus microcarpa"},
        {"species": "樟", "scientific_name": "Cinnamomum camphora"},
        {"species": "茄苳", "scientific_name": "Bischofia javanica"},
    ]
    previous = {
        "schema_version": 1,
        "records": {
            "榕": {"species": "榕", "status": "available", "retrieved_at": "2026-08-02"},
            "樟": {"species": "樟", "status": "available", "retrieved_at": "2026-07-01"},
        },
    }

    assert choose_species(profiles, previous, 2) == ["茄苳", "樟"]


def test_refresh_species_images_uses_scientific_name_and_marks_no_match() -> None:
    profiles = [
        {"species": "榕", "scientific_name": "Ficus microcarpa"},
        {"species": "未知樹", "scientific_name": None},
    ]
    queries: list[str] = []

    def fetch(query: str) -> dict[str, object]:
        queries.append(query)
        return _commons_payload() if query == "Ficus microcarpa" else {"query": {"pages": {}}}

    document = refresh_species_images(
        profiles,
        {"schema_version": 1, "records": {}},
        fetch,
        limit=0,
        clock=lambda: NOW,
        sleeper=lambda _: None,
    )

    assert set(queries) == {"Ficus microcarpa", "未知樹"}
    assert document["total_species"] == 2
    assert document["records"]["榕"]["status"] == "available"
    assert document["records"]["未知樹"] == {
        "species": "未知樹",
        "status": "unavailable",
        "query": "未知樹",
        "retrieved_at": "2026-08-02T12:00:00+00:00",
    }
