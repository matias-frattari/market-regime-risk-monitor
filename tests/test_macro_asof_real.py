"""Checks macro_asof on the REAL local warehouse (not synthetic).

Opens the warehouse read-only. Skipped when the warehouse or the macro_asof view is absent
(CI, or before ``prepare_clean_prices`` has created the views there).
"""

from pathlib import Path

import duckdb
import pytest

from mrm.storage.db import get_db_path
from mrm.storage.schema import MACRO_ASOF_SERIES

DB_PATH = Path(get_db_path())


@pytest.fixture
def warehouse() -> duckdb.DuckDBPyConnection:
    if not DB_PATH.exists():
        pytest.skip("local warehouse not available")
    con = duckdb.connect(str(DB_PATH), read_only=True)
    has_view = con.execute(
        "SELECT count(*) FROM information_schema.tables "
        "WHERE table_name = 'macro_asof' AND table_type = 'VIEW'"
    ).fetchone()[0]
    if not has_view:
        con.close()
        pytest.skip("macro_asof view not created in the local warehouse yet")
    yield con
    con.close()


@pytest.mark.parametrize("series", MACRO_ASOF_SERIES)
def test_one_row_per_trading_day(warehouse: duckdb.DuckDBPyConnection, series: str) -> None:
    n_days = warehouse.execute("SELECT count(DISTINCT trade_date) FROM prices_clean").fetchone()[0]
    n_rows = warehouse.execute(
        "SELECT count(*), count(DISTINCT trade_date) FROM macro_asof WHERE series_id = ?",
        [series],
    ).fetchone()
    assert n_rows == (n_days, n_days)


@pytest.mark.parametrize("series", MACRO_ASOF_SERIES)
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
