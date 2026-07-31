from __future__ import annotations

from datetime import date, datetime
from pathlib import Path

import pandas as pd
import pytest

from scripts import load_postgis


CANONICAL_COLUMNS = [
    "tree_id",
    "district",
    "location",
    "location_note",
    "species",
    "diameter_cm",
    "height_m",
    "survey_date",
    "twd97_x",
    "twd97_y",
    "updated_at",
    "source",
    "snapshot_date",
]


def _frame() -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "tree_id": "TREE-001",
                "district": "大安區",
                "location": "和平東路",
                "location_note": None,
                "species": "榕樹",
                "diameter_cm": 12.5,
                "height_m": float("nan"),
                "survey_date": pd.Timestamp("2026-07-01"),
                "twd97_x": 304123.4,
                "twd97_y": 2765432.1,
                "updated_at": pd.Timestamp("2026-07-02 03:04:05"),
                "source": "street_trees",
                "snapshot_date": pd.Timestamp("2026-07-03"),
            },
            {
                "tree_id": "TREE-002",
                "district": None,
                "location": "忠孝東路",
                "location_note": pd.NaT,
                "species": "樟樹",
                "diameter_cm": 20.0,
                "height_m": 8.0,
                "survey_date": pd.NaT,
                "twd97_x": 305000.0,
                "twd97_y": 2765000.0,
                "updated_at": pd.NaT,
                "source": "street_trees",
                "snapshot_date": pd.Timestamp("2026-07-03"),
            },
        ],
        columns=CANONICAL_COLUMNS,
    )


def _write_parquet(path: Path, frame: pd.DataFrame | None = None) -> Path:
    frame = _frame() if frame is None else frame
    frame.to_parquet(path, index=False)
    return path


class _Result:
    def __init__(self, value: int) -> None:
        self.value = value

    def scalar_one(self) -> int:
        return self.value


class _Connection:
    def __init__(self, counts: list[int], error: Exception | None = None) -> None:
        self.counts = iter(counts)
        self.error = error
        self.executions: list[tuple[str, object | None]] = []
        self.exit_error: tuple[object, object, object] | None = None

    def __enter__(self) -> _Connection:
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> bool:
        self.exit_error = (exc_type, exc, traceback)
        return False

    def execute(self, statement: object, parameters: object | None = None) -> _Result:
        sql = str(statement)
        self.executions.append((sql, parameters))
        if self.error is not None and "INSERT INTO public.trees" in sql:
            raise self.error
        if "COUNT(*)" in sql:
            return _Result(next(self.counts))
        return _Result(0)


class _Engine:
    def __init__(self, connection: _Connection) -> None:
        self.connection = connection
        self.begin_calls = 0
        self.disposed = False

    def begin(self) -> _Connection:
        self.begin_calls += 1
        return self.connection

    def dispose(self) -> None:
        self.disposed = True


def test_load_trees_uses_one_transaction_bound_rows_and_honest_twd97_sql(tmp_path: Path) -> None:
    parquet_path = _write_parquet(tmp_path / "trees.parquet")
    connection = _Connection([1, 1])
    engine = _Engine(connection)

    stats = load_postgis.load_trees("postgresql://user:password@db/trees", parquet_path, lambda _: engine)

    assert stats == load_postgis.LoadStats(inserted=1, updated=1, total=2)
    assert engine.begin_calls == 1
    assert engine.disposed is True
    assert connection.exit_error == (None, None, None)
    all_sql = "\n".join(sql for sql, _ in connection.executions)
    assert "CREATE TABLE IF NOT EXISTS public.trees" in all_sql
    assert "CREATE INDEX IF NOT EXISTS" in all_sql
    assert "CREATE TEMP TABLE" in all_sql
    assert "ON COMMIT DROP" in all_sql
    assert "INSERT INTO public.trees" in all_sql
    assert "ON CONFLICT (source, tree_id) DO UPDATE" in all_sql
    assert "WGS84" not in all_sql
    assert "4326" not in all_sql
    assert "latitude" not in all_sql.casefold()
    assert "longitude" not in all_sql.casefold()
    staged_parameters = next(
        parameters
        for sql, parameters in connection.executions
        if sql.startswith("INSERT INTO") and "public.trees" not in sql
    )
    assert staged_parameters == [
        {
            "tree_id": "TREE-001",
            "district": "大安區",
            "location": "和平東路",
            "location_note": None,
            "species": "榕樹",
            "diameter_cm": 12.5,
            "height_m": None,
            "survey_date": date(2026, 7, 1),
            "twd97_x": 304123.4,
            "twd97_y": 2765432.1,
            "updated_at": datetime(2026, 7, 2, 3, 4, 5),
            "source": "street_trees",
            "snapshot_date": date(2026, 7, 3),
        },
        {
            "tree_id": "TREE-002",
            "district": None,
            "location": "忠孝東路",
            "location_note": None,
            "species": "樟樹",
            "diameter_cm": 20.0,
            "height_m": 8.0,
            "survey_date": None,
            "twd97_x": 305000.0,
            "twd97_y": 2765000.0,
            "updated_at": None,
            "source": "street_trees",
            "snapshot_date": date(2026, 7, 3),
        },
    ]
    assert "TREE-001" not in all_sql
    assert "和平東路" not in all_sql


@pytest.mark.parametrize(
    "frame",
    [
        _frame().drop(columns="species"),
        _frame().assign(unexpected="not allowed"),
        _frame().assign(tree_id=" "),
        _frame().assign(source=" "),
        pd.concat([_frame().iloc[[0]], _frame().iloc[[0]]], ignore_index=True),
    ],
)
def test_load_trees_rejects_invalid_input_before_constructing_engine(
    tmp_path: Path, frame: pd.DataFrame
) -> None:
    parquet_path = _write_parquet(tmp_path / "invalid.parquet", frame)
    engine_factory_calls: list[str] = []

    with pytest.raises(ValueError):
        load_postgis.load_trees(
            "postgresql://user:password@db/trees",
            parquet_path,
            lambda url: engine_factory_calls.append(url),
        )

    assert engine_factory_calls == []


def test_load_trees_rejects_missing_parquet_before_constructing_engine(tmp_path: Path) -> None:
    engine_factory_calls: list[str] = []

    with pytest.raises(ValueError) as error:
        load_postgis.load_trees(
            "postgresql://user:password@db/trees",
            tmp_path / "missing.parquet",
            lambda url: engine_factory_calls.append(url),
        )

    assert "postgresql" not in str(error.value)
    assert engine_factory_calls == []


def test_load_trees_rolls_back_propagates_safely_and_disposes_engine(tmp_path: Path) -> None:
    parquet_path = _write_parquet(tmp_path / "trees.parquet")
    secret_url = "postgresql://user:SENTINEL_PASSWORD@db/trees?token=SENTINEL_QUERY"
    failure = RuntimeError(f"failed while opening {secret_url}")
    connection = _Connection([1, 1], failure)
    engine = _Engine(connection)

    with pytest.raises(RuntimeError) as error:
        load_postgis.load_trees(secret_url, parquet_path, lambda _: engine)

    assert "SENTINEL_PASSWORD" not in str(error.value)
    assert "SENTINEL_QUERY" not in str(error.value)
    assert connection.exit_error is not None
    assert connection.exit_error[0] is RuntimeError
    assert engine.disposed is True


def test_empty_valid_parquet_returns_zero_without_constructing_engine(tmp_path: Path) -> None:
    parquet_path = _write_parquet(tmp_path / "empty.parquet", _frame().iloc[0:0])
    engine_factory_calls: list[str] = []

    stats = load_postgis.load_trees(
        "postgresql://user:password@db/trees",
        parquet_path,
        lambda url: engine_factory_calls.append(url),
    )

    assert stats == load_postgis.LoadStats(0, 0, 0)
    assert engine_factory_calls == []


def test_main_skips_without_database_url_without_constructing_engine(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("DATABASE_URL", "   ")
    monkeypatch.setattr(load_postgis, "load_trees", lambda *args: pytest.fail("must not load"))

    assert load_postgis.main(["--src", "processed/"]) == 0

    output = capsys.readouterr().out
    assert output == "未設定 DATABASE_URL，略過 PostGIS 載入。\n"
    assert "postgresql" not in output.casefold()
