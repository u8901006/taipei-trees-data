"""Accessibility and content contracts for the public search page."""

from pathlib import Path

from bs4 import BeautifulSoup


ROOT = Path(__file__).resolve().parents[1]
SITE = ROOT / "site"


def page() -> BeautifulSoup:
    return BeautifulSoup((SITE / "index.html").read_text(encoding="utf-8"), "html.parser")


def test_page_has_traditional_chinese_metadata_and_landmarks() -> None:
    document = page()
    assert document.html["lang"] == "zh-Hant"
    assert "臺北市行道樹" in document.title.string
    description = document.find("meta", attrs={"name": "description"})
    assert description and 50 <= len(description["content"]) <= 160
    assert document.main is not None
    assert document.main.find("h1") is not None
    assert document.find("a", class_="skip-link")["href"] == "#main-content"


def test_search_form_and_result_table_expose_required_fields() -> None:
    document = page()
    form = document.find("form", id="tree-search")
    assert form and form.get("role") == "search"
    assert form.find("select", id="tree-type") is not None
    assert form.find("select", id="district") is not None
    assert form.find("input", id="location") is not None
    assert form.find("input", id="species") is not None
    assert form.find("button", attrs={"type": "submit"}) is not None
    assert form.find("button", attrs={"type": "reset"}) is not None

    summary = document.find(id="results-summary")
    assert summary and summary.get("aria-live") == "polite"
    assert document.find("tbody", id="results-body") is not None
    headers = [cell.get_text(strip=True) for cell in document.select("#results-table th")]
    assert headers == [
        "行政區",
        "路段／公園",
        "樹種",
        "胸徑",
        "樹高",
        "樹齡",
        "更新日期",
        "地圖／行程",
    ]
    assert document.find(id="previous-page") is not None
    assert document.find(id="next-page") is not None


def test_page_explains_transparency_and_uses_exact_footer_links() -> None:
    document = page()
    text = document.get_text(" ", strip=True)
    assert "資料怎麼來" in text
    assert "資料限制" in text
    assert "GitHub" in text
    assert "可能受影響樹木" in text
    footer_links = {anchor.get("href") for anchor in document.select("footer a")}
    assert {
        "https://www.leepsyclinic.com/",
        "https://blog.leepsyclinic.com/",
        "https://buymeacoffee.com/CYlee",
    } <= footer_links


def test_page_loads_the_module_controller() -> None:
    document = page()
    module = document.find("script", attrs={"type": "module", "src": "./app.js"})
    assert module is not None
    assert "./search.mjs" in (SITE / "app.js").read_text(encoding="utf-8")


def test_page_has_accessible_pruning_schedule_browser() -> None:
    document = page()
    section = document.find("section", id="schedules")

    assert section is not None
    assert section.find("select", id="schedule-type") is not None
    assert section.find("input", id="schedule-query") is not None
    assert section.find(id="schedule-state").get("role") == "status"
    assert section.find(id="schedule-list") is not None
    assert document.find("a", href="#schedules") is not None


def test_page_has_protected_species_and_data_status_interfaces() -> None:
    document = page()
    protected_option = document.select_one('#tree-type option[value="protected"]')

    assert protected_option is not None
    assert "受保護樹木" in protected_option.get_text(strip=True)
    assert document.find(id="protected-tree-details") is not None
    dialog = document.find("dialog", id="species-dialog")
    assert dialog is not None
    assert dialog.find(id="species-dialog-content") is not None
    assert document.find("section", id="data-status") is not None
    for identifier in (
        "protected-count",
        "protected-detail-coverage",
        "species-profile-count",
        "leader-data-status",
    ):
        assert document.find(id=identifier) is not None
