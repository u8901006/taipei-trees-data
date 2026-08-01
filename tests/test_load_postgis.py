from __future__ import annotations

from datetime import date, datetime
from pathlib import Path
import tomllib

import pandas as pd
import pytest
import sqlalchemy

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
    "tree_type",
    "park_name",
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
                "tree_type": "street",
                "park_name": None,
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
                "tree_type": "street",
                "park_name": None,
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
    def __init__(
        self,
        counts: list[int],
        error: Exception | None = None,
        error_marker: str = "INSERT INTO public.trees",
    ) -> None:
        self.counts = iter(counts)
        self.error = error
        self.error_marker = error_marker
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
        if self.error is not None and self.error_marker in sql:
            raise self.error
        if "COUNT(*)" in sql:
            return _Result(next(self.counts))
        return _Result(0)


class _Engine:
    def __init__(self, connection: _Connection, dispose_error: Exception | None = None) -> None:
        self.connection = connection
        self.dispose_error = dispose_error
        self.begin_calls = 0
        self.disposed = False
        self.dispose_calls = 0

    def begin(self) -> _Connection:
        self.begin_calls += 1
        return self.connection

    def dispose(self) -> None:
        self.disposed = True
        self.dispose_calls += 1
        if self.dispose_error is not None:
            raise self.dispose_error


def test_load_trees_uses_one_transaction_bound_rows_and_honest_twd97_sql(tmp_path: Path) -> None:
    parquet_path = _write_parquet(tmp_path / "trees.parquet")
    connection = _Connection([1, 1])
    engine = _Engine(connection)

    stats = load_postgis.load_trees(
        "postgresql://user:password@db/trees", parquet_path, lambda _: engine
    )

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
    assert "DELETE FROM public.trees" in all_sql
    assert "NOT EXISTS" in all_sql
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
            "tree_type": "street",
            "park_name": None,
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
            "tree_type": "street",
            "park_name": None,
        },
    ]
    assert "TREE-001" not in all_sql
    assert "和平東路" not in all_sql
    delete_sql, delete_parameters = next(
        (sql, parameters)
        for sql, parameters in connection.executions
        if "DELETE FROM public.trees" in sql
    )
    assert "street_trees" not in delete_sql
    assert "TREE-001" not in delete_sql
    assert delete_parameters == {"reconciled_sources": ["street_trees"]}


def test_load_trees_normalizes_postgresql_url_for_psycopg_engine_factory(tmp_path: Path) -> None:
    parquet_path = _write_parquet(tmp_path / "trees.parquet")
    connection = _Connection([1, 1])
    engine = _Engine(connection)
    received_urls: list[str] = []

    def engine_factory(database_url: str) -> _Engine:
        received_urls.append(database_url)
        return engine

    load_postgis.load_trees(
        "postgresql://user:secret@host/db?application_name=tree-loader",
        parquet_path,
        engine_factory,
    )

    assert received_urls == [
        "postgresql+psycopg://user:secret@host/db?application_name=tree-loader"
    ]


def test_single_missing_tree_is_reconciled_after_current_rows_are_upserted(
    tmp_path: Path,
) -> None:
    parquet_path = _write_parquet(
        tmp_path / "trees.parquet",
        _frame().iloc[[0]],
    )
    connection = _Connection([0, 1])
    engine = _Engine(connection)

    stats = load_postgis.load_trees(
        "postgresql://user:password@db/trees",
        parquet_path,
        lambda _: engine,
    )

    assert stats == load_postgis.LoadStats(inserted=0, updated=1, total=1)
    statements = [sql for sql, _ in connection.executions]
    upsert_index = next(
        index for index, sql in enumerate(statements) if "INSERT INTO public.trees" in sql
    )
    delete_index = next(
        index for index, sql in enumerate(statements) if "DELETE FROM public.trees" in sql
    )
    assert upsert_index < delete_index
    _, parameters = connection.executions[delete_index]
    assert parameters == {"reconciled_sources": ["street_trees"]}


def test_postgresql_psycopg_url_is_preserved_and_other_schemes_are_safely_rejected(
    tmp_path: Path,
) -> None:
    parquet_path = _write_parquet(tmp_path / "trees.parquet")
    received_urls: list[str] = []
    connection = _Connection([1, 1])
    engine = _Engine(connection)

    load_postgis.load_trees(
        "postgresql+psycopg://user:secret@host/db?application_name=tree-loader",
        parquet_path,
        lambda database_url: (received_urls.append(database_url), engine)[1],
    )

    assert received_urls == [
        "postgresql+psycopg://user:secret@host/db?application_name=tree-loader"
    ]
    for unsupported_url in (
        "sqlite:///SENTINEL_PASSWORD.db",
        "mysql://user:SENTINEL_PASSWORD@host/db?token=SENTINEL_QUERY",
    ):
        with pytest.raises(ValueError) as error:
            load_postgis.load_trees(
                unsupported_url, parquet_path, lambda _: pytest.fail("no engine")
            )

        assert str(error.value) == "不支援的資料庫連線設定"
        assert "SENTINEL_PASSWORD" not in str(error.value)
        assert "SENTINEL_QUERY" not in str(error.value)


def test_sqlalchemy_can_construct_psycopg_engine_without_connecting() -> None:
    engine = sqlalchemy.create_engine("postgresql+psycopg://user:pass@localhost/db")

    try:
        assert engine.dialect.driver == "psycopg"
    finally:
        engine.dispose()


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


def test_reconciliation_failure_rolls_back_upsert_and_delete_together(
    tmp_path: Path,
) -> None:
    parquet_path = _write_parquet(
        tmp_path / "trees.parquet",
        _frame().iloc[[0]],
    )
    failure = RuntimeError("delete failed with sensitive detail")
    connection = _Connection(
        [0, 1],
        failure,
        error_marker="DELETE FROM public.trees",
    )
    engine = _Engine(connection)

    with pytest.raises(RuntimeError, match="資料庫載入失敗"):
        load_postgis.load_trees(
            "postgresql://user:password@db/trees",
            parquet_path,
            lambda _: engine,
        )

    assert connection.exit_error is not None
    assert connection.exit_error[0] is RuntimeError
    assert engine.begin_calls == 1
    assert engine.disposed is True


@pytest.mark.parametrize("transaction_fails", [False, True])
def test_load_trees_hides_dispose_failure_without_replacing_transaction_failure(
    tmp_path: Path, transaction_fails: bool
) -> None:
    parquet_path = _write_parquet(tmp_path / "trees.parquet")
    secret_url = "postgresql://user:SENTINEL_PASSWORD@db/trees?token=SENTINEL_QUERY"
    transaction_error = RuntimeError(f"transaction failed for {secret_url}")
    dispose_error = RuntimeError(f"dispose failed for {secret_url}")
    connection = _Connection([1, 1], transaction_error if transaction_fails else None)
    engine = _Engine(connection, dispose_error)

    with pytest.raises(RuntimeError) as error:
        load_postgis.load_trees(secret_url, parquet_path, lambda _: engine)

    assert str(error.value) == "資料庫載入失敗"
    assert "SENTINEL_PASSWORD" not in str(error.value)
    assert "SENTINEL_QUERY" not in str(error.value)
    assert engine.dispose_calls == 1
    if transaction_fails:
        assert connection.exit_error is not None
        assert connection.exit_error[0] is RuntimeError
    else:
        assert connection.exit_error == (None, None, None)


@pytest.mark.parametrize(
    ("filename", "source"),
    [
        ("trees.parquet", "street_trees"),
        ("protected_trees.parquet", "protected_trees"),
        ("park_trees.parquet", "park_trees"),
    ],
)
def test_empty_canonical_snapshot_clears_its_source_in_one_transaction(
    tmp_path: Path,
    filename: str,
    source: str,
) -> None:
    parquet_path = _write_parquet(tmp_path / filename, _frame().iloc[0:0])
    connection = _Connection([])
    engine = _Engine(connection)

    stats = load_postgis.load_trees(
        "postgresql://user:password@db/trees",
        parquet_path,
        lambda _: engine,
    )

    assert stats == load_postgis.LoadStats(0, 0, 0)
    assert engine.begin_calls == 1
    delete_sql, delete_parameters = next(
        (sql, parameters)
        for sql, parameters in connection.executions
        if "DELETE FROM public.trees" in sql
    )
    assert source not in delete_sql
    assert delete_parameters == {"reconciled_sources": [source]}


def test_empty_snapshot_with_ambiguous_filename_is_rejected_before_engine(
    tmp_path: Path,
) -> None:
    parquet_path = _write_parquet(tmp_path / "empty.parquet", _frame().iloc[0:0])
    engine_factory_calls: list[str] = []

    with pytest.raises(ValueError, match="來源"):
        load_postgis.load_trees(
            "postgresql://user:password@db/trees",
            parquet_path,
            lambda url: engine_factory_calls.append(url),
        )

    assert engine_factory_calls == []


def test_empty_valid_parquet_rejects_unsupported_database_scheme_before_engine(
    tmp_path: Path,
) -> None:
    parquet_path = _write_parquet(tmp_path / "empty.parquet", _frame().iloc[0:0])
    engine_factory_calls: list[str] = []

    with pytest.raises(ValueError, match="不支援") as error:
        load_postgis.load_trees(
            "sqlite:///SENTINEL_PASSWORD.db",
            parquet_path,
            lambda url: engine_factory_calls.append(url),
        )

    assert "SENTINEL_PASSWORD" not in str(error.value)
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


def test_main_src_loads_street_and_existing_protected_snapshots(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    processed = tmp_path / "processed"
    processed.mkdir()
    street = processed / "trees.parquet"
    protected = processed / "protected_trees.parquet"
    street.touch()
    protected.touch()
    calls: list[tuple[str, Path]] = []

    def fake_load(database_url: str, path: Path) -> load_postgis.LoadStats:
        calls.append((database_url, path))
        return (
            load_postgis.LoadStats(2, 3, 5) if path == street else load_postgis.LoadStats(7, 11, 18)
        )

    monkeypatch.setenv("DATABASE_URL", "postgresql://db/trees")
    monkeypatch.setattr(load_postgis, "load_trees", fake_load)

    assert load_postgis.main(["--src", str(processed)]) == 0

    assert calls == [
        ("postgresql://db/trees", street),
        ("postgresql://db/trees", protected),
    ]
    assert capsys.readouterr().out == "新增 9 筆，更新 14 筆，共 23 筆。\n"


def test_main_explicit_parquet_loads_only_that_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    explicit = tmp_path / "protected_trees.parquet"
    explicit.touch()
    calls: list[Path] = []
    monkeypatch.setenv("DATABASE_URL", "postgresql://db/trees")
    monkeypatch.setattr(
        load_postgis,
        "load_trees",
        lambda _database_url, path: calls.append(path) or load_postgis.LoadStats(0, 0, 0),
    )

    assert load_postgis.main(["--src", str(tmp_path), "--parquet", str(explicit)]) == 0

    assert calls == [explicit]


def test_postgresql_driver_is_declared_for_runtime_loading() -> None:
    repository = Path(__file__).resolve().parents[1]
    project = tomllib.loads((repository / "pyproject.toml").read_text(encoding="utf-8"))
    requirements = (repository / "scripts" / "requirements.txt").read_text(encoding="utf-8")

    assert "psycopg[binary]>=3.2,<4" in project["project"]["dependencies"]
    assert "psycopg[binary]>=3.2,<4" in requirements.splitlines()
