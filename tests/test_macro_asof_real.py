"""Checks macro_asof on the REAL local warehouse (not synthetic).

Opens the warehouse read-only. Skipped when the warehouse, the macro_asof view or the
macro_publication_lags table is absent (CI, or before ``prepare_clean_prices`` has run there).
"""

from datetime import timedelta
from pathlib import Path

import duckdb
import pytest

from mrm.config import load_macro_lags
from mrm.storage.db import get_db_path
from tests.legacy_macro_asof import DAILY_SERIES, current_daily_rows, legacy_rows

DB_PATH = Path(get_db_path())
LAGS = {lag.series_id: lag for lag in load_macro_lags()}


@pytest.fixture
def warehouse() -> duckdb.DuckDBPyConnection:
    if not DB_PATH.exists():
        pytest.skip("local warehouse not available")
    con = duckdb.connect(str(DB_PATH), read_only=True)
    has_view = con.execute(
        "SELECT count(*) FROM information_schema.tables "
        "WHERE (table_name = 'macro_asof' AND table_type = 'VIEW') "
        "OR table_name = 'macro_publication_lags'"
    ).fetchone()[0]
    if has_view < 2:
        con.close()
        pytest.skip("macro_asof / macro_publication_lags not created in the local warehouse yet")
    yield con
    con.close()


@pytest.mark.parametrize("series", sorted(LAGS))
def test_one_row_per_trading_day(warehouse: duckdb.DuckDBPyConnection, series: str) -> None:
    n_days = warehouse.execute("SELECT count(DISTINCT trade_date) FROM prices_clean").fetchone()[0]
    n_rows = warehouse.execute(
        "SELECT count(*), count(DISTINCT trade_date) FROM macro_asof WHERE series_id = ?",
        [series],
    ).fetchone()
    assert n_rows == (n_days, n_days)


@pytest.mark.parametrize("series", DAILY_SERIES)
def test_filled_rows_match_fred_missing_days(
    warehouse: duckdb.DuckDBPyConnection, series: str
) -> None:
    # Strict rule: a row is filled when FRED had '.' or no row on the previous trading day,
    # so the count equals the missing FRED days among all trading days except the last one.
    expected = warehouse.execute(
        """
        WITH td AS (SELECT DISTINCT trade_date FROM prices_clean)
        SELECT count(*)
        FROM td
        LEFT JOIN raw_macro m ON m.series_id = ? AND m.obs_date = td.trade_date
        WHERE m.value IS NULL AND td.trade_date < (SELECT max(trade_date) FROM td)
        """,
        [series],
    ).fetchone()[0]
    filled = warehouse.execute(
        "SELECT count(*) FILTER (WHERE is_filled) FROM macro_asof WHERE series_id = ?",
        [series],
    ).fetchone()[0]
    assert filled == expected


def test_daily_series_identical_to_phase_1_6_view(warehouse: duckdb.DuckDBPyConnection) -> None:
    legacy = legacy_rows(warehouse)
    assert legacy
    assert current_daily_rows(warehouse) == legacy  # exact equality, values included


def test_synced_lags_match_config(warehouse: duckdb.DuckDBPyConnection) -> None:
    synced = dict(
        warehouse.execute("SELECT series_id, lag_days FROM macro_publication_lags").fetchall()
    )
    assert synced == {s: lag.lag_days for s, lag in LAGS.items()}


@pytest.mark.parametrize("series", sorted(LAGS))
def test_values_used_only_after_their_release(
    warehouse: duckdb.DuckDBPyConnection, series: str
) -> None:
    # Independent of the view internals: recompute the release date from the config.
    lag = LAGS[series]
    overrides = {o.obs_date: o.available_date for o in lag.overrides}
    rows = warehouse.execute(
        "SELECT trade_date, value_date, available_date FROM macro_asof "
        "WHERE series_id = ? AND value IS NOT NULL",
        [series],
    ).fetchall()
    assert rows
    for trade_date, value_date, available_date in rows:
        release = overrides.get(value_date, value_date + timedelta(days=lag.lag_days))
        assert release <= available_date < trade_date
