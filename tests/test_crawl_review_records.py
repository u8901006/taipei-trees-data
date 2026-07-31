from __future__ import annotations

import json
from contextlib import contextmanager
from datetime import date
from pathlib import Path

import httpx
import pytest

from scripts.io_utils import ImmutableSnapshotError

from scripts.crawl_review_records import _eligible_title, crawl_records, main, parse_roc_date


ROOT = "https://culture.gov.taipei/News.aspx?n=22311D615C1DFA8E&sms=C4203F8E019F7B1B"
PAGE_2 = "https://culture.gov.taipei/News.aspx?n=22311D615C1DFA8E&sms=C4203F8E019F7B1B&page=2"
DETAIL_ONE = "https://culture.gov.taipei/News_Content.aspx?n=22311D615C1DFA8E&s=review-one"
DETAIL_TWO = "https://culture.gov.taipei/News_Content.aspx?n=22311D615C1DFA8E&s=review-two"
PDF_ONE = "https://culture.gov.taipei/files/meeting-one.pdf"
PDF_TWO = "https://culture.gov.taipei/files/meeting-two"


class FakeClient:
    def __init__(self, routes: dict[str, httpx.Response | list[httpx.Response]]) -> None:
        self.routes = routes
        self.calls: list[str] = []

    def _response(self, url: str) -> httpx.Response:
        self.calls.append(url)
        response = self.routes[url]
        if isinstance(response, list):
            return response.pop(0)
        return response

    def get(self, url: str, **kwargs: object) -> httpx.Response:
        return self._response(url)

    @contextmanager
    def stream(self, method: str, url: str, **kwargs: object):
        yield self._response(url)


def response(content: bytes | str, content_type: str = "text/html") -> httpx.Response:
    content = content.encode("utf-8") if isinstance(content, str) else content
    return httpx.Response(200, content=content, headers={"content-type": content_type})


def fixture(name: str) -> str:
    return (Path(__file__).parent / "fixtures" / name).read_text(encoding="utf-8")


def routes() -> dict[str, httpx.Response]:
    return {
        ROOT: response(fixture("meeting_index.html")),
        PAGE_2: response(fixture("meeting_index_page2.html")),
        DETAIL_ONE: response(fixture("meeting_detail.html")),
        "https://culture.gov.taipei/News_Content.aspx?n=22311D615C1DFA8E&s=committee-one": response('<a href="/files/committee.pdf">PDF</a>'),
        DETAIL_TWO: response('<a href="/files/meeting-three.pdf">PDF</a>'),
        PDF_ONE: response(b"%PDF-1.7 first", "application/pdf"),
        PDF_TWO: response(b"%PDF-1.7 second", "application/pdf"),
        "https://culture.gov.taipei/files/meeting-three.pdf": response(b"%PDF-1.7 third", "application/pdf"),
        "https://culture.gov.taipei/files/committee.pdf": response(b"%PDF-1.7 committee", "application/pdf"),
    }


@pytest.mark.parametrize("value, expected", [("115-7-1", date(2026, 7, 1)), ("115.07.02", date(2026, 7, 2)), ("115/7/03", date(2026, 7, 3))])
def test_parse_roc_date_accepts_only_valid_roc_dates(value: str, expected: date) -> None:
    assert parse_roc_date(value) == expected


@pytest.mark.parametrize("value", ["115-07", "115-13-01", "115-02-30", "2026-07-01", "115_07_01"])
def test_parse_roc_date_rejects_invalid_or_non_roc_dates(value: str) -> None:
    with pytest.raises(ValueError):
        parse_roc_date(value)


@pytest.mark.parametrize(
    ("title", "review", "committee"),
    [
        ("〖會議紀錄〗115.07.08臺北市樹木保護委員會第15屆第22次幹事會會議紀錄", True, False),
        ("〖會議記錄〗105.07.28第10屆臺北市樹木保護委員會第16次專案小組暨第16次幹事會會議紀錄", True, False),
        ("〖會議紀錄〗115.03.18臺北市樹木保護委員會第15屆第2次委員會會議紀錄", False, True),
        ("臺北市樹木保護委員會會議議程", False, False),
        ("臺北市樹木保護委員會現場會勘紀錄", False, False),
    ],
)
def test_real_official_title_taxonomy_routes_staff_and_project_groups_to_review_only(
    title: str, review: bool, committee: bool
) -> None:
    assert _eligible_title(title, "review") is review
    assert _eligible_title(title, "committee") is committee


def test_crawl_paginates_once_resolves_relative_urls_and_filters_review(tmp_path: Path) -> None:
    client = FakeClient(routes())
    records = crawl_records(ROOT, tmp_path, "review", client)
    assert [record.path.relative_to(tmp_path).as_posix() for record in records] == [
        "2026-07/臺北市樹木保護委員會第 1 次幹事會會議紀錄.pdf",
        "2026-07/臺北市樹木保護委員會第 1 次幹事會會議紀錄__2.pdf",
        "2026-08/臺北市樹木保護委員會第 5 次幹事會會議紀錄.pdf",
    ]
    assert client.calls.count(ROOT) == 1
    assert client.calls.count(PAGE_2) == 1
    assert PDF_ONE in client.calls and PDF_TWO in client.calls
    assert all("evil.example" not in call for call in client.calls)


def test_crawl_respects_page_limit_and_committee_excludes_review_titles(tmp_path: Path) -> None:
    client = FakeClient(routes())
    records = crawl_records(ROOT, tmp_path, "committee", client, max_pages=1)
    assert len(records) == 1
    assert records[0].title == "樹木保護委員會第 2 次會議紀錄"
    assert PAGE_2 not in client.calls


@pytest.mark.parametrize(
    ("pdf_response", "message"),
    [
        (response(b"not a PDF", "application/pdf"), "PDF"),
        (response(b"%PDF-1.7 html", "text/html"), "HTML"),
        (
            httpx.Response(
                200,
                content=b"%PDF-1.7 short",
                headers={"content-type": "application/pdf", "content-length": str(100 * 1024 * 1024 + 1)},
            ),
            "100 MiB",
        ),
    ],
)
def test_pdf_validation_rejects_non_pdf_html_or_excessive_content_before_storing(
    tmp_path: Path, pdf_response: httpx.Response, message: str
) -> None:
    client = FakeClient(routes() | {PDF_ONE: pdf_response})
    with pytest.raises(ValueError, match=message):
        crawl_records(ROOT, tmp_path, "review", client, max_pages=1)
    assert not list(tmp_path.rglob("*.pdf"))


def test_safe_unicode_multi_attachment_immutable_and_manifest_consistency(tmp_path: Path) -> None:
    title = "臺北／樹木：會議紀錄"
    detail = "https://culture.gov.taipei/detail"
    pdf = "https://culture.gov.taipei/a.pdf"
    index = f'<tr><td>115-07-01</td><td><a href="{detail}">樹木保護委員會幹事會{title}</a></td></tr>'
    client = FakeClient({ROOT: response(index), detail: response(f'<a href="{pdf}">PDF</a>'), pdf: response(b"%PDF-1.7 x", "application/pdf")})
    first = crawl_records(ROOT, tmp_path, "review", client)
    assert first[0].path.name == "樹木保護委員會幹事會臺北_樹木_會議紀錄.pdf"
    manifest_path = first[0].path.with_suffix(".manifest.json")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert set(manifest) == {"schema_version", "title", "published_date", "detail_url", "attachment_url", "sha256", "byte_length", "retrieved_at"}
    second_client = FakeClient({ROOT: response(index), detail: response(f'<a href="{pdf}">PDF</a>'), pdf: response(b"%PDF-1.7 x", "application/pdf")})
    assert crawl_records(ROOT, tmp_path, "review", second_client)[0].status == "unchanged"
    manifest["title"] = "tampered"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ImmutableSnapshotError, match="manifest"):
        crawl_records(ROOT, tmp_path, "review", FakeClient({ROOT: response(index), detail: response(f'<a href="{pdf}">PDF</a>'), pdf: response(b"%PDF-1.7 x", "application/pdf")}))


def test_hash_duplicate_uses_existing_path_without_second_file(tmp_path: Path) -> None:
    first_index = '<tr><td>115-07-01</td><td><a href="https://culture.gov.taipei/one">樹木保護委員會幹事會會議紀錄</a></td></tr>'
    second_index = '<tr><td>115-07-02</td><td><a href="https://culture.gov.taipei/two">樹木保護委員會第 2 次幹事會會議紀錄</a></td></tr>'
    one = "https://culture.gov.taipei/one"; two = "https://culture.gov.taipei/two"
    pdf_one = "https://culture.gov.taipei/one.pdf"; pdf_two = "https://culture.gov.taipei/two.pdf"
    payload = b"%PDF-1.7 same"
    crawl_records(ROOT, tmp_path, "review", FakeClient({ROOT: response(first_index), one: response(f'<a href="{pdf_one}">PDF</a>'), pdf_one: response(payload, "application/pdf")}))
    duplicate = crawl_records(ROOT, tmp_path, "review", FakeClient({ROOT: response(second_index), two: response(f'<a href="{pdf_two}">PDF</a>'), pdf_two: response(payload, "application/pdf")}))[0]
    assert duplicate.status == "duplicate"
    assert duplicate.path.name == "樹木保護委員會幹事會會議紀錄.pdf"
    assert len(list(tmp_path.rglob("*.pdf"))) == 1


def test_invalid_or_sensitive_urls_fail_closed_without_echoing_secret(tmp_path: Path) -> None:
    secret = "do-not-leak"
    invalid = f"https://person:{secret}@culture.gov.taipei/News.aspx"
    with pytest.raises(ValueError) as error:
        crawl_records(invalid, tmp_path, "review", FakeClient({}))
    assert secret not in str(error.value)


def test_cli_writes_exact_new_files_and_skips_unavailable_optional_source(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    config = tmp_path / "sources.json"
    config.write_text(json.dumps({"review_records": {"url": None, "required": False}}), encoding="utf-8")
    github_output = tmp_path / "github-output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(github_output))
    assert main(["--out", str(tmp_path / "raw"), "--kind", "review", "--config", str(config)]) == 0
    assert github_output.read_text(encoding="utf-8") == "new_files=0\n"
    assert "unavailable" in capsys.readouterr().out
