"""Optionally load a normalized tree Parquet snapshot into PostGIS."""

from __future__ import annotations

import argparse
import os
import uuid
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Callable, Sequence

import pandas as pd
import sqlalchemy

from scripts.normalize import CANONICAL_COLUMNS


@dataclass(frozen=True, slots=True)
class LoadStats:
    inserted: int
    updated: int
    total: int


_DATE_COLUMNS = {"survey_date", "snapshot_date"}
_TEXT_COLUMNS = {"tree_id", "district", "location", "location_note", "species", "source"}
_UPDATE_COLUMNS = [column for column in CANONICAL_COLUMNS if column not in {"tree_id", "source"}]


def _safe_parquet_frame(parquet_path: Path) -> pd.DataFrame:
    if not parquet_path.is_file():
        raise ValueError("找不到或無法讀取 Parquet 檔案")
    try:
        frame = pd.read_parquet(parquet_path)
    except Exception as error:
        raise ValueError("無法讀取 Parquet 檔案") from error

    actual_columns = list(frame.columns)
    if set(actual_columns) != set(CANONICAL_COLUMNS) or len(actual_columns) != len(CANONICAL_COLUMNS):
        raise ValueError("Parquet 欄位必須完全符合 canonical 13 欄")
    return frame


def _validate_keys(frame: pd.DataFrame) -> None:
    for column in ("tree_id", "source"):
        values = frame[column]
        if values.isna().any() or values.astype(str).str.strip().eq("").any():
            raise ValueError(f"{column} 不可為空白")
    if frame.duplicated(["source", "tree_id"]).any():
        raise ValueError("(source, tree_id) 不可重複")


def _is_null(value: object) -> bool:
    if value is None:
        return True
    try:
        missing = pd.isna(value)
    except (TypeError, ValueError):
        return False
    return not hasattr(missing, "__len__") and bool(missing)


def _standard_value(column: str, value: object) -> object | None:
    if _is_null(value):
        return None
    if isinstance(value, pd.Timestamp):
        value = value.to_pydatetime()
    if column in _DATE_COLUMNS:
        if isinstance(value, datetime):
            return value.date()
        if isinstance(value, date):
            return value
        return date.fromisoformat(str(value))
    if column == "updated_at":
        if isinstance(value, datetime):
            return value
        if isinstance(value, date):
            return datetime.combine(value, datetime.min.time())
        return datetime.fromisoformat(str(value))
    if column in _TEXT_COLUMNS:
        return str(value)
    return value


def _bound_rows(frame: pd.DataFrame) -> list[dict[str, object | None]]:
    return [
        {column: _standard_value(column, row[column]) for column in CANONICAL_COLUMNS}
        for row in frame.loc[:, CANONICAL_COLUMNS].to_dict(orient="records")
    ]


def _schema_sql() -> tuple[str, ...]:
    return (
        """
        CREATE TABLE IF NOT EXISTS public.trees (
            tree_id text NOT NULL,
            district text,
            location text,
            location_note text,
            species text,
            diameter_cm double precision,
            height_m double precision,
            survey_date date,
            twd97_x double precision,
            twd97_y double precision,
            updated_at timestamp without time zone,
            source text NOT NULL,
            snapshot_date date,
            loaded_at timestamp with time zone NOT NULL DEFAULT now(),
            PRIMARY KEY (source, tree_id)
        )
        """,
        "CREATE INDEX IF NOT EXISTS trees_source_snapshot_idx ON public.trees (source, snapshot_date)",
        "CREATE INDEX IF NOT EXISTS trees_species_idx ON public.trees (species)",
    )


def load_trees(
    database_url: str,
    parquet_path: Path,
    engine_factory: Callable[[str], object] = sqlalchemy.create_engine,
) -> LoadStats:
    """Validate canonical rows and publish them in one transaction."""
    frame = _safe_parquet_frame(parquet_path)
    _validate_keys(frame)
    rows = _bound_rows(frame)
    if not rows:
        return LoadStats(inserted=0, updated=0, total=0)

    engine: object | None = None
    stats: LoadStats | None = None
    failure: RuntimeError | None = None
    try:
        engine = engine_factory(database_url)
        staging_table = f"tree_stage_{uuid.uuid4().hex}"
        staging_columns = ", ".join(CANONICAL_COLUMNS)
        parameter_columns = ", ".join(f":{column}" for column in CANONICAL_COLUMNS)
        update_set = ", ".join(f"{column} = EXCLUDED.{column}" for column in _UPDATE_COLUMNS)
        with engine.begin() as connection:  # type: ignore[union-attr]
            for statement in _schema_sql():
                connection.execute(sqlalchemy.text(statement))
            connection.execute(
                sqlalchemy.text(
                    f"CREATE TEMP TABLE {staging_table} "
                    "(LIKE public.trees INCLUDING DEFAULTS) ON COMMIT DROP"
                )
            )
            connection.execute(
                sqlalchemy.text(
                    f"INSERT INTO {staging_table} ({staging_columns}) VALUES ({parameter_columns})"
                ),
                rows,
            )
            inserted = connection.execute(
                sqlalchemy.text(
                    f"SELECT COUNT(*) FROM {staging_table} AS stage "
                    "LEFT JOIN public.trees AS target "
                    "ON target.source = stage.source AND target.tree_id = stage.tree_id "
                    "WHERE target.tree_id IS NULL"
                )
            ).scalar_one()
            updated = connection.execute(
                sqlalchemy.text(
                    f"SELECT COUNT(*) FROM {staging_table} AS stage "
                    "JOIN public.trees AS target "
                    "ON target.source = stage.source AND target.tree_id = stage.tree_id"
                )
            ).scalar_one()
            connection.execute(
                sqlalchemy.text(
                    f"INSERT INTO public.trees ({staging_columns}) "
                    f"SELECT {staging_columns} FROM {staging_table} "
                    "ON CONFLICT (source, tree_id) DO UPDATE SET "
                    f"{update_set}"
                )
            )
            stats = LoadStats(inserted=inserted, updated=updated, total=len(rows))
    except Exception:
        failure = RuntimeError("資料庫載入失敗")
    finally:
        if engine is not None:
            try:
                engine.dispose()  # type: ignore[union-attr]
            except Exception:
                if failure is None:
                    failure = RuntimeError("資料庫載入失敗")
    if failure is not None:
        raise failure from None
    assert stats is not None
    return stats


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--src", type=Path, default=Path("processed"))
    parser.add_argument("--parquet", type=Path)
    arguments = parser.parse_args(argv)
    database_url = os.environ.get("DATABASE_URL", "")
    if not database_url.strip():
        print("未設定 DATABASE_URL，略過 PostGIS 載入。")
        return 0
    parquet_path = arguments.parquet or arguments.src / "trees.parquet"
    try:
        stats = load_trees(database_url, parquet_path)
    except Exception:
        print("PostGIS 載入失敗。")
        return 1
    print(f"新增 {stats.inserted} 筆，更新 {stats.updated} 筆，共 {stats.total} 筆。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
