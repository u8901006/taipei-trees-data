"""GitHub Actions workflow contracts are parsed as YAML, not string-matched."""

from __future__ import annotations

import re
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml


ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / ".github" / "workflows"
SCHEDULES = {
    "daily-opendata.yml": "0 19 * * *",
    "health-check.yml": "0 1 * * *",
    "weekly-schedule.yml": "20 1 * * *",
    "monthly-review.yml": "0 2 5,20 * *",
    "quarterly-committee.yml": "0 2 10,25 1,4,7,10 *",
    "gap-report.yml": "0 2 1 * *",
}
EXPECTED_WORKFLOW_FILES = set(SCHEDULES) | {"ci.yml", "pages.yml"}
DIRECT_WRITERS = {
    "daily-opendata.yml": {
        "commands": (
            'python scripts/fetch_opendata.py --out raw/open_data/ --date "${{ steps.taipei-date.outputs.value }}"',
            "python scripts/normalize.py --raw raw/open_data/ --out processed/",
            "python scripts/detect_anomalies.py --processed processed/ --out reports/anomalies.json",
        ),
        "git_add": "git add raw/open_data/ processed/ reports/anomalies.json",
    },
    "weekly-schedule.yml": {
        "commands": (
            "python scripts/fetch_schedule.py --out raw/pruning_schedules/ --processed-out processed/pruning_schedule.json",
        ),
        "git_add": "git add raw/pruning_schedules/ processed/pruning_schedule.json",
    },
    "health-check.yml": {
        "commands": ("python scripts/health_check.py --out reports/health.json",),
        "git_add": "git add reports/health.json",
    },
    "gap-report.yml": {
        "commands": (
            "python scripts/gap_report.py --health reports/health.json --out reports/gaps.json",
        ),
        "git_add": "git add reports/gaps.json",
    },
}
ALLOWED_ACTIONS = {
    "actions/checkout@v4",
    "actions/setup-python@v5",
    "actions/setup-node@v4",
    "actions/github-script@v7",
    "actions/upload-artifact@v4",
    "actions/configure-pages@v5",
    "actions/upload-pages-artifact@v4",
    "actions/deploy-pages@v4",
    "peter-evans/create-pull-request@v6",
}


class WorkflowLoader(yaml.SafeLoader):
    """Keep GitHub's ``on`` key as a string under PyYAML 1.1 semantics."""


for key, resolvers in list(WorkflowLoader.yaml_implicit_resolvers.items()):
    WorkflowLoader.yaml_implicit_resolvers[key] = [
        resolver for resolver in resolvers if resolver[0] != "tag:yaml.org,2002:bool"
    ]
WorkflowLoader.add_implicit_resolver(
    "tag:yaml.org,2002:bool",
    re.compile(r"^(?:true|false)$", re.IGNORECASE),
    list("tTfF"),
)


def load_workflow(filename: str) -> dict[str, object]:
    with (WORKFLOWS / filename).open(encoding="utf-8") as workflow_file:
        document = yaml.load(workflow_file, Loader=WorkflowLoader)
    assert isinstance(document, dict)
    return document


def workflow_text(filename: str) -> str:
    return (WORKFLOWS / filename).read_text(encoding="utf-8")


def all_steps(workflow: dict[str, object]) -> list[dict[str, object]]:
    jobs = workflow["jobs"]
    assert isinstance(jobs, dict)
    steps: list[dict[str, object]] = []
    for job in jobs.values():
        assert isinstance(job, dict)
        job_steps = job.get("steps", [])
        assert isinstance(job_steps, list)
        steps.extend(step for step in job_steps if isinstance(step, dict))
    return steps


def run_blocks(workflow: dict[str, object]) -> list[str]:
    return [run for step in all_steps(workflow) if isinstance((run := step.get("run")), str)]


def values_contain_secret_context(value: object) -> bool:
    if isinstance(value, str):
        return "${{ secrets." in value
    if isinstance(value, dict):
        return any(values_contain_secret_context(item) for item in value.values())
    if isinstance(value, list):
        return any(values_contain_secret_context(item) for item in value)
    return False


def test_all_scheduled_workflows_have_exact_quoted_crons_and_manual_trigger() -> None:
    assert {path.name for path in WORKFLOWS.glob("*.yml")} == EXPECTED_WORKFLOW_FILES
    for filename, cron in SCHEDULES.items():
        workflow = load_workflow(filename)
        trigger = workflow["on"]
        assert isinstance(trigger, dict)
        assert trigger.get("workflow_dispatch") == {}
        assert trigger["schedule"] == [{"cron": cron}]
        assert f'cron: "{cron}"' in workflow_text(filename)


def test_all_jobs_use_pinned_runtime_actions_and_explicit_timeouts() -> None:
    for filename in [*SCHEDULES, "ci.yml", "pages.yml"]:
        workflow = load_workflow(filename)
        jobs = workflow["jobs"]
        assert isinstance(jobs, dict)
        for job in jobs.values():
            assert isinstance(job, dict)
            assert job["runs-on"] == "ubuntu-latest"
            assert isinstance(job.get("timeout-minutes"), int)
            assert 0 < job["timeout-minutes"] <= 60
        uses = {step["uses"] for step in all_steps(workflow) if "uses" in step}
        assert uses <= ALLOWED_ACTIONS
        assert "actions/checkout@v4" in uses
        assert "actions/setup-python@v5" in uses
        setup = next(
            step for step in all_steps(workflow) if step.get("uses") == "actions/setup-python@v5"
        )
        assert setup["with"]["python-version"] == "3.12"


def test_daily_concurrency_permissions_commands_and_anomaly_issue_contract() -> None:
    workflow = load_workflow("daily-opendata.yml")
    assert workflow["permissions"] == {"contents": "write", "issues": "write"}
    assert workflow["concurrency"] == {"group": "data-sync", "cancel-in-progress": False}
    text = workflow_text("daily-opendata.yml")
    assert "python scripts/fetch_opendata.py --out raw/open_data/" in text
    assert "python scripts/normalize.py --raw raw/open_data/ --out processed/" in text
    assert (
        "python scripts/detect_anomalies.py --processed processed/ --out reports/anomalies.json"
        in text
    )
    assert "git add raw/open_data/ processed/ reports/anomalies.json" in text
    assert "git pull --rebase origin" in text
    assert "actions/github-script@v7" in text
    assert "steps.anomalies.outputs.found == 'true'" in text
    assert "reports/anomalies.json" in text
    assert "anomaly-detected" in text
    assert "臺北樹木資料異常" in text
    assert "DATABASE_URL" in text
    assert "github.event_name != 'pull_request'" in text
    assert "github.repository_owner == github.event.repository.owner.login" in text


def test_daily_publishes_git_archive_before_optional_postgis_from_published_revision() -> None:
    steps = all_steps(load_workflow("daily-opendata.yml"))
    archive_index = next(
        index for index, step in enumerate(steps) if step.get("id") == "publish-archive"
    )
    anomaly_issue_index = next(
        index for index, step in enumerate(steps) if step.get("id") == "anomaly-issue"
    )
    secret_index = next(
        index for index, step in enumerate(steps) if step.get("id") == "database-secret"
    )
    database_index = next(
        index for index, step in enumerate(steps) if step.get("id") == "load-database"
    )
    archive_run = str(steps[archive_index]["run"])
    database_run = str(steps[database_index]["run"])

    assert archive_index < anomaly_issue_index < secret_index < database_index
    assert steps[anomaly_issue_index]["if"] == "${{ steps.anomalies.outputs.found == 'true' }}"
    assert "git commit " in archive_run
    assert "git push origin " in archive_run
    assert "git fetch --no-tags origin" in database_run
    assert 'origin "+${GITHUB_REF_NAME}:refs/remotes/origin/${GITHUB_REF_NAME}"' in database_run
    assert 'git rev-parse "origin/${GITHUB_REF_NAME}"' in database_run
    assert database_run.index("git fetch --no-tags origin") < database_run.index(
        "python scripts/load_postgis.py --src processed/"
    )


def test_daily_passes_taipei_calendar_date_across_utc_day_boundary() -> None:
    boundary = datetime(2026, 7, 30, 16, 30, tzinfo=UTC)
    assert boundary.astimezone(ZoneInfo("Asia/Taipei")).date().isoformat() == "2026-07-31"

    steps = all_steps(load_workflow("daily-opendata.yml"))
    date_index = next(index for index, step in enumerate(steps) if step.get("id") == "taipei-date")
    fetch_index = next(
        index
        for index, step in enumerate(steps)
        if "scripts/fetch_opendata.py" in str(step.get("run", ""))
    )
    date_step = steps[date_index]
    fetch_run = str(steps[fetch_index]["run"])

    assert date_index < fetch_index
    assert date_step["env"] == {"TZ": "Asia/Taipei"}
    assert "value=$(date +%F)" in str(date_step["run"])
    assert '--date "${{ steps.taipei-date.outputs.value }}"' in fetch_run


def test_extraction_workflows_install_pdf_and_traditional_chinese_ocr_packages() -> None:
    required_packages = {
        "poppler-utils",
        "tesseract-ocr",
        "tesseract-ocr-chi-tra",
    }
    for filename in ("monthly-review.yml", "quarterly-committee.yml"):
        steps = all_steps(load_workflow(filename))
        install_steps = [step for step in steps if "apt-get install" in str(step.get("run", ""))]
        assert len(install_steps) == 1
        run = str(install_steps[0]["run"])
        assert "sudo apt-get update" in run
        assert required_packages <= set(run.split())


def test_all_direct_base_writers_share_non_cancelling_data_sync_concurrency() -> None:
    for filename in DIRECT_WRITERS:
        assert load_workflow(filename)["concurrency"] == {
            "group": "data-sync",
            "cancel-in-progress": False,
        }


def test_all_mutating_scheduled_workflows_have_non_cancelling_concurrency() -> None:
    for filename in SCHEDULES:
        workflow = load_workflow(filename)
        concurrency = workflow.get("concurrency")
        assert isinstance(concurrency, dict)
        assert isinstance(concurrency.get("group"), str) and concurrency["group"]
        assert concurrency.get("cancel-in-progress") is False


def test_extraction_workflows_create_human_review_prs_without_direct_extract_push() -> None:
    for filename, kind in (
        ("monthly-review.yml", "review"),
        ("quarterly-committee.yml", "committee"),
    ):
        workflow = load_workflow(filename)
        assert workflow["permissions"] == {"contents": "write", "pull-requests": "write"}
        text = workflow_text(filename)
        assert (
            f"python scripts/crawl_review_records.py --out raw/review_meetings/ --kind {kind}"
            in text
        )
        assert "python scripts/extract_cases.py --in raw/review_meetings/ --out extracted/" in text
        assert "peter-evans/create-pull-request@v6" in text
        assert "needs-human-review" in text
        assert "case number, address, decision, tree count, date, page, and quote" in text
        assert "git add extracted/" not in text
        assert "ANTHROPIC_API_KEY" in text
        assert "github.event_name != 'pull_request'" in text
        assert "github.repository_owner == github.event.repository.owner.login" in text


def test_weekly_health_and_gap_use_narrow_git_paths() -> None:
    weekly = workflow_text("weekly-schedule.yml")
    assert (
        "python scripts/fetch_schedule.py --out raw/pruning_schedules/ --processed-out processed/pruning_schedule.json"
        in weekly
    )
    assert "git add raw/pruning_schedules/ processed/pruning_schedule.json" in weekly
    health = workflow_text("health-check.yml")
    assert "python scripts/health_check.py --out reports/health.json" in health
    assert "git add reports/health.json" in health
    gaps = workflow_text("gap-report.yml")
    assert (
        "python scripts/gap_report.py --health reports/health.json --out reports/gaps.json" in gaps
    )
    assert "git add reports/gaps.json" in gaps


def test_direct_writer_commands_git_add_and_push_rebase_are_structurally_safe() -> None:
    forbidden_adds = {"git add .", "git add ./", "git add -A", "git add --all"}
    for filename, contract in DIRECT_WRITERS.items():
        workflow = load_workflow(filename)
        blocks = run_blocks(workflow)
        for command in contract["commands"]:
            assert any(command in block.splitlines() for block in blocks)
        add_lines = {
            line.strip()
            for block in blocks
            for line in block.splitlines()
            if line.strip().startswith("git add ")
        }
        assert add_lines == {contract["git_add"]}
        assert add_lines.isdisjoint(forbidden_adds)
        push_blocks = [block for block in blocks if "git push " in block]
        assert len(push_blocks) == 1
        push_block = push_blocks[0]
        assert "for attempt in 1 2 3" in push_block
        assert push_block.index("git pull --rebase origin") < push_block.index("git push origin")


def test_secret_availability_uses_step_output_gate_and_never_secret_in_if() -> None:
    expected_secret_steps = {
        "daily-opendata.yml": ("database-secret", "DATABASE_URL", "load-database"),
        "monthly-review.yml": ("anthropic-secret", "ANTHROPIC_API_KEY", "extract-cases"),
        "quarterly-committee.yml": ("anthropic-secret", "ANTHROPIC_API_KEY", "extract-cases"),
    }
    for filename, (gate_id, secret_name, consumer_id) in expected_secret_steps.items():
        steps = all_steps(load_workflow(filename))
        gate = next(step for step in steps if step.get("id") == gate_id)
        consumer = next(step for step in steps if step.get("id") == consumer_id)
        assert gate["env"] == {"OPTIONAL_SECRET": f"${{{{ secrets.{secret_name} }}}}"}
        assert "secrets." not in str(gate.get("if", ""))
        assert "available=true" in gate["run"]
        assert "available=false" in gate["run"]
        assert str(consumer["if"]).find(f"steps.{gate_id}.outputs.available == 'true'") >= 0
        assert "secrets." not in str(consumer["if"])
        assert consumer["env"] == {secret_name: f"${{{{ secrets.{secret_name} }}}}"}

    for filename in EXPECTED_WORKFLOW_FILES:
        for step in all_steps(load_workflow(filename)):
            assert "secrets." not in str(step.get("if", ""))
            assert not values_contain_secret_context(step.get("run"))
            assert not values_contain_secret_context(step.get("with"))
            for key, value in step.items():
                if key != "env":
                    assert not values_contain_secret_context(value)


def test_artifacts_and_action_majors_are_parsed_and_safe() -> None:
    for filename in SCHEDULES:
        steps = all_steps(load_workflow(filename))
        artifact_steps = [
            step for step in steps if step.get("uses") == "actions/upload-artifact@v4"
        ]
        assert len(artifact_steps) == 1
        artifact = artifact_steps[0]
        assert artifact["if"] == "${{ failure() }}"
        assert artifact["with"]["path"] == "reports/"
        assert artifact["with"]["if-no-files-found"] == "ignore"
        assert all(step.get("uses") in ALLOWED_ACTIONS for step in steps if step.get("uses"))


def test_ci_and_dependabot_contracts_are_safe_and_complete() -> None:
    ci = load_workflow("ci.yml")
    assert ci["permissions"] == {"contents": "read"}
    assert set(ci["on"]) == {"push", "pull_request"}
    ci_text = workflow_text("ci.yml")
    for command in (
        "python -m pytest -q",
        "python -m compileall -q scripts tests",
        "python -m ruff check scripts tests",
        "python -m ruff format --check scripts tests",
        "node --test tests/site-search.test.mjs",
    ):
        assert command in ci_text
    assert "cache: pip" in ci_text
    assert "actions/setup-node@v4" in ci_text


def test_pages_workflow_builds_real_search_data_and_deploys_safely() -> None:
    workflow = load_workflow("pages.yml")
    assert workflow["permissions"] == {
        "contents": "read",
        "pages": "write",
        "id-token": "write",
    }
    trigger = workflow["on"]
    assert trigger["workflow_dispatch"] == {}
    assert trigger["schedule"] == [{"cron": "30 20 * * *"}]
    assert trigger["push"]["branches"] == ["main"]
    assert workflow["concurrency"] == {
        "group": "github-pages",
        "cancel-in-progress": False,
    }

    steps = all_steps(workflow)
    uses = {step.get("uses") for step in steps}
    assert {
        "actions/configure-pages@v5",
        "actions/upload-pages-artifact@v4",
        "actions/deploy-pages@v4",
    } <= uses
    text = workflow_text("pages.yml")
    for command in (
        "python scripts/fetch_opendata.py",
        "python scripts/normalize.py",
        "python scripts/fetch_schedule.py",
        "python scripts/build_site_data.py",
        "python scripts/validate_site_data.py",
        "node --test tests/site-search.test.mjs",
    ):
        assert command in text
    assert "--park-src processed/park_trees.parquet" in text
    assert "--schedule processed/pruning_schedule.json" in text
    upload = next(step for step in steps if step.get("uses") == "actions/upload-pages-artifact@v4")
    assert upload["with"]["path"] == "_site"
    deploy = next(step for step in steps if step.get("uses") == "actions/deploy-pages@v4")
    assert deploy["id"] == "deployment"

    with (ROOT / ".github" / "dependabot.yml").open(encoding="utf-8") as dependabot_file:
        dependabot = yaml.load(dependabot_file, Loader=WorkflowLoader)
    assert dependabot["version"] == 2
    ecosystems = {entry["package-ecosystem"]: entry for entry in dependabot["updates"]}
    assert set(ecosystems) == {"pip", "github-actions"}
    assert all(entry["schedule"]["interval"] == "weekly" for entry in ecosystems.values())
    assert all(entry["open-pull-requests-limit"] <= 10 for entry in ecosystems.values())
    assert all(entry["commit-message"]["prefix"] == "deps" for entry in ecosystems.values())


def test_workflows_forbid_unsafe_events_permissions_and_secret_exposure() -> None:
    for path in WORKFLOWS.glob("*.yml"):
        text = path.read_text(encoding="utf-8")
        assert "pull_request_target" not in text
        assert "write-all" not in text
        assert "eval " not in text
        assert "env |" not in text
        assert "upload-artifact" not in text or "path: reports/" in text
        assert "git add ." not in text
        assert "git add -A" not in text
        assert "git add --all" not in text
