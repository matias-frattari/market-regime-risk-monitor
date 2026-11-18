"""Checks the XLF 2016-09-19 correction on the REAL local warehouse (not synthetic).

Opens the warehouse read-only (no views can be created there). Skipped when the warehouse
or the prices_clean view is absent (CI, or before `mrm check` has prepared the clean layer).
"""

from datetime import date
from pathlib import Path

import duckdb
import pytest

from mrm.storage.db import get_db_path

DB_PATH = Path(get_db_path())
EX_DATE = date(2016, 9, 19)


@pytest.fixture
def warehouse() -> duckdb.DuckDBPyConnection:
    if not DB_PATH.exists():
        pytest.skip("local warehouse not available")
    con = duckdb.connect(str(DB_PATH), read_only=True)
    has_view = con.execute(
        "SELECT count(*) FROM information_schema.tables "
        "WHERE table_name = 'prices_clean' AND table_type = 'VIEW'"
    ).fetchone()[0]
    if not has_view:
        con.close()
        pytest.skip("prices_clean view not created in the local warehouse yet")
    yield con
    con.close()


def _return_on(con: duckdb.DuckDBPyConnection, table: str, day: date) -> float:
    rows = con.execute(
        f"SELECT close FROM {table} WHERE ticker = 'XLF' AND trade_date <= ? "
        "ORDER BY trade_date DESC LIMIT 2",
        [day],
    ).fetchall()
    assert len(rows) == 2, f"XLF data around {day} missing in {table}"
    return rows[0][0] / rows[1][0] - 1


def test_xlf_ex_date_return_below_1pct_in_prices_clean(
    warehouse: duckdb.DuckDBPyConnection,
) -> None:
    clean_return = _return_on(warehouse, "prices_clean", EX_DATE)
    assert abs(clean_return) < 0.01, f"prices_clean XLF return on {EX_DATE}: {clean_return:+.4%}"


def test_raw_prices_untouched(warehouse: duckdb.DuckDBPyConnection) -> None:
    # The correction lives in the view only; raw_prices keeps the provider's data.
    raw_return = _return_on(warehouse, "raw_prices", EX_DATE)
    applied = warehouse.execute(
        "SELECT applied FROM price_adjustment_status WHERE ticker = 'XLF' AND ex_date = ?",
        [EX_DATE],
    ).fetchone()
    assert applied is not None, "XLF adjustment not synced (run `uv run mrm check`)"
    if applied[0]:
        assert raw_return > 0.10
