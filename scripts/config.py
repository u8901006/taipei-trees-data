"""載入可稽核的資料來源設定。"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping


@dataclass(frozen=True, slots=True)
class SourceConfig:
    """單一資料來源的可用性與必要性設定。"""

    name: str
    url: str | None
    dataset_id: str | None
    required: bool

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError("來源名稱不可為空白")
        if not isinstance(self.required, bool):
            raise TypeError("required 必須是布林值")
        for field_name, value in (("url", self.url), ("dataset_id", self.dataset_id)):
            if value is not None and (not isinstance(value, str) or not value.strip()):
                raise ValueError(f"{field_name} 必須是非空白字串或 None")

    @property
    def available(self) -> bool:
        """來源具有 URL 或資料集 ID 時才可由後續管線擷取。"""
        return self.url is not None or self.dataset_id is not None


_ENVIRONMENT_OVERRIDES: dict[str, tuple[str, str]] = {
    "street_trees": ("TAIPEI_STREET_TREES_ID", "dataset_id"),
    "park_trees": ("TAIPEI_PARK_TREES_URL", "url"),
    "protected_trees": ("TAIPEI_PROTECTED_TREES_ID", "dataset_id"),
    "pruning_schedule": ("TAIPEI_PRUNING_SCHEDULE_URL", "url"),
    "review_records": ("TAIPEI_REVIEW_RECORDS_URL", "url"),
    "committee_records": ("TAIPEI_COMMITTEE_RECORDS_URL", "url"),
}


def load_sources(path: Path, env: Mapping[str, str]) -> dict[str, SourceConfig]:
    """讀取 JSON 來源清單，並套用明確定義的 repository variable 覆寫。"""
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("sources.json 的根節點必須是物件")

    sources: dict[str, SourceConfig] = {}
    for name, raw_source in data.items():
        if not isinstance(raw_source, dict):
            raise ValueError(f"來源 {name!r} 必須是物件")

        source_data = dict(raw_source)
        environment_override = _ENVIRONMENT_OVERRIDES.get(name)
        if environment_override is not None:
            environment_name, field_name = environment_override
            override = env.get(environment_name)
            if override and override.strip():
                source_data[field_name] = override

        sources[name] = SourceConfig(
            name=name,
            url=source_data.get("url"),
            dataset_id=source_data.get("dataset_id"),
            required=source_data.get("required", False),
        )
    return sources
