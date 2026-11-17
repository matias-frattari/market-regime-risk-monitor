"""DuckDB connection helpers."""

import os
from pathlib import Path

import duckdb

from mrm.config import REPO_ROOT

DB_PATH_ENV = "MRM_DB_PATH"
DEFAULT_DB_PATH: Path = REPO_ROOT / "data" / "warehouse.duckdb"
IN_MEMORY = ":memory:"


def get_db_path() -> str:
    """Return the warehouse path: ``$MRM_DB_PATH`` if set, else ``data/warehouse.duckdb``.

    Only reads the process environment; loading ``.env`` is the caller's job.
    """
    value = os.environ.get(DB_PATH_ENV, "").strip()
    return value or str(DEFAULT_DB_PATH)


def connect(path: str | Path | None = None) -> duckdb.DuckDBPyConnection:
    """Open the warehouse (``":memory:"`` allowed), creating the parent directory if needed."""
    target = str(path) if path is not None else get_db_path()
    if target != IN_MEMORY:
        Path(target).parent.mkdir(parents=True, exist_ok=True)
    return duckdb.connect(target)
