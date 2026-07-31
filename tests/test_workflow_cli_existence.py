"""Final integration contract between workflows and application CLIs."""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_every_workflow_cli_target_exists() -> None:
    targets = {
        "scripts/fetch_opendata.py",
        "scripts/normalize.py",
        "scripts/detect_anomalies.py",
        "scripts/load_postgis.py",
        "scripts/fetch_schedule.py",
        "scripts/crawl_review_records.py",
        "scripts/extract_cases.py",
        "scripts/health_check.py",
        "scripts/gap_report.py",
    }
    assert {target for target in targets if not (ROOT / target).is_file()} == set()
