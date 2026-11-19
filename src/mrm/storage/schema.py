"""Warehouse DDL. Every statement is idempotent (IF NOT EXISTS; views use OR REPLACE).

All timestamp columns hold naive UTC values written by the loaders.
No foreign keys: in DuckDB they would block updates of dim_asset; integrity is
checked in the loaders and tests instead.
"""

import duckdb

TABLES: tuple[str, ...] = (
    "dim_asset",
    "raw_prices",
    "raw_macro",
    "ingest_log",
    "dq_results",
    "price_adjustments",
)
VIEWS: tuple[str, ...] = ("price_adjustment_status", "prices_clean", "macro_asof")

# Daily FRED series exposed in macro_asof. Series with publication lags (CPIAUCSL, UNRATE,
# FEDFUNDS, NFCI, STLFSI4) are pending their own ADR.
MACRO_ASOF_SERIES: tuple[str, ...] = ("DGS10", "DGS2", "DGS3MO", "T10Y2Y")
_MACRO_ASOF_VALUES = ", ".join(f"('{s}')" for s in MACRO_ASOF_SERIES)

DDL: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS dim_asset (
        ticker      VARCHAR   NOT NULL PRIMARY KEY,
        asset_class VARCHAR   NOT NULL,
        updated_at  TIMESTAMP NOT NULL  -- UTC
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS raw_prices (
        ticker      VARCHAR   NOT NULL,
        trade_date  DATE      NOT NULL,
        open        DOUBLE,
        high        DOUBLE,
        low         DOUBLE,
        close       DOUBLE    NOT NULL,
        volume      BIGINT,
        source      VARCHAR   NOT NULL,
        ingested_at TIMESTAMP NOT NULL,  -- UTC
        PRIMARY KEY (ticker, trade_date)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS raw_macro (
        series_id   VARCHAR   NOT NULL,
        obs_date    DATE      NOT NULL,
        value       DOUBLE,
        source      VARCHAR   NOT NULL,
        ingested_at TIMESTAMP NOT NULL,  -- UTC
        PRIMARY KEY (series_id, obs_date)
    )
    """,
    "CREATE SEQUENCE IF NOT EXISTS ingest_log_seq START 1",
    """
    CREATE TABLE IF NOT EXISTS ingest_log (
        log_id      BIGINT    NOT NULL PRIMARY KEY DEFAULT nextval('ingest_log_seq'),
        source      VARCHAR   NOT NULL,
        entity      VARCHAR   NOT NULL,
        date_from   DATE,
        date_to     DATE,
        rows_loaded INTEGER   NOT NULL,
        status      VARCHAR   NOT NULL CHECK (status IN ('success', 'no_data', 'error')),
        message     VARCHAR,
        logged_at   TIMESTAMP NOT NULL  -- UTC
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS dq_results (
        run_id      VARCHAR   NOT NULL,
        run_at      TIMESTAMP NOT NULL,  -- UTC
        check_name  VARCHAR   NOT NULL,
        entity      VARCHAR   NOT NULL,
        severity    VARCHAR   NOT NULL CHECK (severity IN ('critical', 'warning', 'info')),
        n_affected  INTEGER   NOT NULL,
        detail      VARCHAR,
        PRIMARY KEY (run_id, check_name, entity, severity)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS price_adjustments (
        adjustment_id             VARCHAR   NOT NULL PRIMARY KEY,
        ticker                    VARCHAR   NOT NULL,
        ex_date                   DATE      NOT NULL,
        multiplier                DOUBLE    NOT NULL CHECK (multiplier > 0),
        apply_if_raw_return_above DOUBLE    NOT NULL,
        synced_at                 TIMESTAMP NOT NULL  -- UTC
    )
    """,
    # Raw simple return on each ex_date vs the previous stored trading day of the ticker.
    # An adjustment applies only while that raw return is above its threshold.
    """
    CREATE OR REPLACE VIEW price_adjustment_status AS
    WITH raw_returns AS (
        SELECT
            ticker,
            trade_date,
            close,
            lag(trade_date) OVER w AS prev_date,
            lag(close) OVER w AS prev_close
        FROM raw_prices
        WINDOW w AS (PARTITION BY ticker ORDER BY trade_date)
    )
    SELECT
        a.adjustment_id,
        a.ticker,
        a.ex_date,
        a.multiplier,
        a.apply_if_raw_return_above,
        r.prev_date,
        r.prev_close,
        r.close AS ex_close,
        r.close / r.prev_close - 1 AS raw_return,
        coalesce(r.close / r.prev_close - 1 > a.apply_if_raw_return_above, false) AS applied
    FROM price_adjustments a
    LEFT JOIN raw_returns r ON r.ticker = a.ticker AND r.trade_date = a.ex_date
    """,
    # raw_prices with applied adjustments: OHLC before each ex_date times its multiplier
    # (product when several apply). Same rows as raw_prices; volume is never corrected.
    """
    CREATE OR REPLACE VIEW prices_clean AS
    WITH factors AS (
        SELECT
            p.ticker,
            p.trade_date,
            product(s.multiplier) AS factor,
            string_agg(s.adjustment_id, ',' ORDER BY s.ex_date) AS adjustment_ids
        FROM raw_prices p
        JOIN price_adjustment_status s
            ON s.applied AND s.ticker = p.ticker AND p.trade_date < s.ex_date
        GROUP BY p.ticker, p.trade_date
    )
    SELECT
        p.ticker,
        p.trade_date,
        p.open * coalesce(f.factor, 1.0) AS open,
        p.high * coalesce(f.factor, 1.0) AS high,
        p.low * coalesce(f.factor, 1.0) AS low,
        p.close * coalesce(f.factor, 1.0) AS close,
        p.volume,
        p.source,
        p.ingested_at,
        coalesce(f.factor, 1.0) AS adjustment_factor,
        f.adjustment_ids
    FROM raw_prices p
    LEFT JOIN factors f USING (ticker, trade_date)
    """,
    # Point-in-time daily macro on the trading calendar of prices_clean, long format.
    # Strict rule obs_date < trade_date: daily yields are published the next business day,
    # so day d uses the latest non-missing observation dated before d (forward-fill only
    # forward; NULL when the series has no earlier value). is_filled marks values carried
    # over a FRED '.' or a missing row on the previous trading day.
    f"""
    CREATE OR REPLACE VIEW macro_asof AS
    WITH calendar AS (
        SELECT trade_date, lag(trade_date) OVER (ORDER BY trade_date) AS prev_trade_date
        FROM (SELECT DISTINCT trade_date FROM prices_clean)
    ),
    series AS (
        SELECT series_id FROM (VALUES {_MACRO_ASOF_VALUES}) v(series_id)
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
    """,
)


def create_schema(con: duckdb.DuckDBPyConnection) -> None:
    """Create all warehouse objects if they do not exist yet."""
    for statement in DDL:
        con.execute(statement)
