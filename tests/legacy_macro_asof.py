"""Frozen Phase 1.6 definition of ``macro_asof`` (commit b734466), for non-regression tests.

ADR 0005 extended the view with lagged series; for the four daily series (lag 0) the new
view must return exactly these rows. Do not edit: this is the reference, not live code.
"""

import duckdb

DAILY_SERIES: tuple[str, ...] = ("DGS10", "DGS2", "DGS3MO", "T10Y2Y")
_VALUES = ", ".join(f"('{s}')" for s in DAILY_SERIES)

LEGACY_MACRO_ASOF_SQL = f"""
    WITH calendar AS (
        SELECT trade_date, lag(trade_date) OVER (ORDER BY trade_date) AS prev_trade_date
        FROM (SELECT DISTINCT trade_date FROM prices_clean)
    ),
    series AS (
        SELECT series_id FROM (VALUES {_VALUES}) v(series_id)
    ),
    grid AS (
        SELECT c.trade_date, c.prev_trade_date, s.series_id
        FROM calendar c CROSS JOIN series s
    ),
    obs AS (
        SELECT series_id, obs_date, value
        FROM raw_macro
        WHERE value IS NOT NULL AND series_id IN (SELECT series_id FROM series)
    )
    SELECT
        g.trade_date,
        g.series_id,
        o.value,
        o.obs_date AS value_date,
        coalesce(o.obs_date < g.prev_trade_date, false) AS is_filled
    FROM grid g
    ASOF LEFT JOIN obs o
        ON o.series_id = g.series_id AND g.trade_date > o.obs_date
"""

_COLUMNS = "trade_date, series_id, value, value_date, is_filled"


def legacy_rows(con: duckdb.DuckDBPyConnection) -> list[tuple]:
    """Rows of the frozen Phase 1.6 view, sorted."""
    return con.execute(f"SELECT {_COLUMNS} FROM ({LEGACY_MACRO_ASOF_SQL}) ORDER BY ALL").fetchall()


def current_daily_rows(con: duckdb.DuckDBPyConnection) -> list[tuple]:
    """Same columns of the live ``macro_asof`` for the four daily series, sorted."""
    placeholders = ", ".join("?" for _ in DAILY_SERIES)
    return con.execute(
        f"SELECT {_COLUMNS} FROM macro_asof WHERE series_id IN ({placeholders}) ORDER BY ALL",
        list(DAILY_SERIES),
    ).fetchall()
