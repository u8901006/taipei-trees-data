"""Final integration contract between workflow commands and application CLIs."""

import re
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / ".github" / "workflows"


def test_every_workflow_cli_target_exists() -> None:
    targets: set[str] = set()
    for workflow_path in WORKFLOWS.glob("*.yml"):
        workflow = yaml.safe_load(workflow_path.read_text(encoding="utf-8"))
        for job in workflow.get("jobs", {}).values():
            for step in job.get("steps", []):
                run = step.get("run")
                if isinstance(run, str):
                    targets.update(
                        re.findall(r"(?<!\S)python\s+(scripts/[A-Za-z0-9_./-]+\.py)\b", run)
                    )
    assert targets
    assert {target for target in targets if not (ROOT / target).is_file()} == set()
