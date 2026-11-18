"""Warehouse DDL. Every statement is idempotent (IF NOT EXISTS).

All timestamp columns hold naive UTC values written by the loaders.
No foreign keys: in DuckDB they would block updates of dim_asset; integrity is
checked in the loaders and tests instead.
"""

import duckdb

TABLES: tuple[str, ...] = ("dim_asset", "raw_prices", "raw_macro", "ingest_log", "dq_results")

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
)


def create_schema(con: duckdb.DuckDBPyConnection) -> None:
    """Create all warehouse objects if they do not exist yet."""
    for statement in DDL:
        con.execute(statement)
