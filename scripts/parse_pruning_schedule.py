"""Parse official Taipei street and park pruning schedule HTML."""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from datetime import date, datetime
from typing import Iterable, Literal
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup


Category = Literal["street", "park"]
_DATE = re.compile(r"^(\d{2,4})[./-](\d{1,2})[./-](\d{1,2})$")
_MONTH_DAY = re.compile(r"^(\d{1,2})月(\d{1,2})日$")
_INTEGER = re.compile(r"\d[\d,]*")
_SPLIT = re.compile(r"[、,，；;\n]+")
_STREET_REQUIRED = frozenset(
    {"開始日期", "結束日期", "分隊", "地點", "工作項目", "工作內容", "數量", "施作單位", "依據"}
)
_PARK_REQUIRED = frozenset({"類別", "行政區", "預定日期", "道路/工程名稱", "負責單位"})
_HEADER_ALIASES = {
    "作業期間(起)": "開始日期",
    "作業期間(迄)": "結束日期",
    "施工項目": "工作項目",
    "施工內容": "工作內容",
    "預定修剪日期": "預定日期",
    "預定修剪路段及項目": "道路/工程名稱",
    "承辦單位": "負責單位",
}


class ScheduleParseError(ValueError):
    """A fixed-message official-page parsing failure."""


def _failure() -> ScheduleParseError:
    return ScheduleParseError("schedule parse failed")


def _decode(content: bytes) -> str:
    for encoding in ("utf-8-sig", "utf-8", "cp950", "big5"):
        try:
            return content.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise _failure()


def _clean(value: object) -> str:
    return re.sub(r"\s+", " ", unicodedata.normalize("NFKC", str(value))).strip()


def _official_url(url: str) -> bool:
    try:
        parsed = urlsplit(url)
    except (TypeError, ValueError):
        return False
    host = parsed.hostname.casefold() if parsed.hostname else ""
    return (
        parsed.scheme.casefold() == "https"
        and parsed.username is None
        and parsed.password is None
        and not parsed.fragment
        and (host == "gov.taipei" or host.endswith(".gov.taipei"))
    )


def discover_schedule_urls(html: bytes, base_url: str) -> dict[str, str]:
    """Discover exactly one current street and park pruning page from the index."""
    if not _official_url(base_url):
        raise _failure()
    soup = BeautifulSoup(_decode(html), "html.parser")
    matches: dict[str, list[str]] = {"street": [], "park": []}
    for anchor in soup.find_all("a", href=True):
        label = _clean(anchor.get_text(" ", strip=True))
        category: str | None = None
        if label in {"公園樹木預定修剪行程", "公園樹木預定修剪行程表"}:
            category = "park"
        elif label in {
            "行道樹預定修剪行程",
            "園藝工程隊行道樹修剪預定路段一覽表",
        }:
            category = "street"
        if category is None:
            continue
        target = urljoin(base_url, str(anchor["href"]))
        if not _official_url(target):
            raise _failure()
        matches[category].append(target)
    if any(len(urls) != 1 for urls in matches.values()):
        raise _failure()
    return {category: urls[0] for category, urls in matches.items()}


def _iso_date(value: object, year_hint: int | None = None) -> str:
    match = _DATE.fullmatch(_clean(value))
    if match is not None:
        year, month, day = (int(part) for part in match.groups())
        year = year if year >= 1911 else year + 1911
    else:
        month_day = _MONTH_DAY.fullmatch(_clean(value))
        if month_day is None or year_hint is None:
            raise _failure()
        year = year_hint
        month, day = (int(part) for part in month_day.groups())
    try:
        return date(year, month, day).isoformat()
    except ValueError as error:
        raise _failure() from error


def _parts(value: object) -> list[str]:
    return [part for part in (_clean(item) for item in _SPLIT.split(str(value))) if part]


def _count(value: object) -> int | None:
    match = _INTEGER.search(_clean(value))
    return int(match.group().replace(",", "")) if match else None


def _requester_type(basis: str | None) -> str | None:
    if not basis:
        return None
    if "里長" in basis:
        return "village_chief_recommendation"
    if "議員" in basis:
        return "councillor_case"
    if "承商" in basis:
        return "contractor_report"
    return "other"


def _table_rows(html: bytes, required: frozenset[str]) -> list[dict[str, str]]:
    soup = BeautifulSoup(_decode(html), "html.parser")
    for table in soup.find_all("table"):
        rows = table.find_all("tr")
        if not rows:
            continue
        headers = [
            _HEADER_ALIASES.get(value, value)
            for value in (
                _clean(cell.get_text(" ", strip=True)) for cell in rows[0].find_all(["th", "td"])
            )
        ]
        if not required.issubset(headers):
            continue
        parsed: list[dict[str, str]] = []
        rowspans: dict[int, tuple[str, int]] = {}
        for row in rows[1:]:
            source_cells = iter(row.find_all(["th", "td"]))
            cells: list[str] = []
            for column in range(len(headers)):
                if column in rowspans:
                    value, remaining = rowspans[column]
                    cells.append(value)
                    if remaining == 1:
                        del rowspans[column]
                    else:
                        rowspans[column] = (value, remaining - 1)
                    continue
                try:
                    cell = next(source_cells)
                except StopIteration:
                    cells.append("")
                    continue
                value = _clean(cell.get_text(" ", strip=True))
                cells.append(value)
                try:
                    remaining = int(cell.get("rowspan", 1)) - 1
                except (TypeError, ValueError):
                    raise _failure() from None
                if remaining > 0:
                    rowspans[column] = (value, remaining)
            if not any(cells):
                continue
            parsed.append(dict(zip(headers, cells, strict=True)))
        if parsed:
            return parsed
    raise _failure()


def _page_year(html: bytes) -> int | None:
    text = _clean(BeautifulSoup(_decode(html), "html.parser").get_text(" ", strip=True))
    match = re.search(r"(?:資料更新|製表日期)[：:]?\s*(\d{2,3})[年./-]", text)
    if match is None:
        return None
    year = int(match.group(1))
    return year if year >= 1911 else year + 1911


def _park_locations(value: object) -> list[str]:
    locations: list[str] = []
    for part in _parts(value):
        match = re.match(r"^(.+?(?:公園|綠地|廣場))(?:[（(\s-].*|喬木.*|草皮.*|$)", part)
        locations.append(match.group(1) if match else part)
    return locations


def _schedule_id(item: dict[str, object]) -> str:
    stable = {
        key: value for key, value in item.items() if key not in {"schedule_id", "retrieved_at"}
    }
    encoded = json.dumps(stable, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:24]


def parse_schedule(
    html: bytes,
    category: Category,
    source_url: str,
    retrieved_at: datetime,
) -> list[dict[str, object]]:
    """Parse one official schedule table into the common evidence contract."""
    if category not in {"street", "park"} or not _official_url(source_url):
        raise _failure()
    if retrieved_at.tzinfo is None or retrieved_at.utcoffset() is None:
        raise _failure()
    required = _STREET_REQUIRED if category == "street" else _PARK_REQUIRED
    rows = _table_rows(html, required)
    year_hint = _page_year(html)
    schedules: list[dict[str, object]] = []
    seen_ids: set[str] = set()
    for row in rows:
        if category == "street":
            basis = row["依據"] or None
            item: dict[str, object] = {
                "category": category,
                "start_date": _iso_date(row["開始日期"]),
                "end_date": _iso_date(row["結束日期"]),
                "districts": [],
                "locations": _parts(row["地點"]),
                "team": row["分隊"] or None,
                "work_type": row["工作項目"] or None,
                "work_detail": row["工作內容"] or None,
                "planned_count": _count(row["數量"]),
                "work_unit": row["施作單位"] or None,
                "basis": basis,
                "requester_type": _requester_type(basis),
                "requester_name": None,
            }
        else:
            planned_date = _iso_date(row["預定日期"], year_hint)
            item = {
                "category": category,
                "start_date": planned_date,
                "end_date": planned_date,
                "districts": _parts(row["行政區"]),
                "locations": _park_locations(row["道路/工程名稱"]),
                "team": None,
                "work_type": row["類別"] or "公園樹木",
                "work_detail": None,
                "planned_count": None,
                "work_unit": row["負責單位"] or None,
                "basis": None,
                "requester_type": None,
                "requester_name": None,
            }
        if not item["locations"]:
            raise _failure()
        item.update(
            {
                "source_url": source_url,
                "published_at": None,
                "retrieved_at": retrieved_at.isoformat(),
            }
        )
        item["schedule_id"] = _schedule_id(item)
        if str(item["schedule_id"]) in seen_ids:
            continue
        seen_ids.add(str(item["schedule_id"]))
        schedules.append({"schedule_id": item.pop("schedule_id"), **item})
    return schedules


def build_schedule_document(
    schedules: Iterable[dict[str, object]], retrieved_at: datetime
) -> dict[str, object]:
    """Build a deterministic versioned schedule document."""
    if retrieved_at.tzinfo is None or retrieved_at.utcoffset() is None:
        raise _failure()
    ordered = sorted(
        (dict(item) for item in schedules),
        key=lambda item: (str(item.get("start_date", "")), str(item.get("schedule_id", ""))),
    )
    return {
        "schema_version": 1,
        "retrieved_at": retrieved_at.isoformat(),
        "schedules": ordered,
    }
