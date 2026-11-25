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
    "macro_publication_lags",
    "macro_release_overrides",
    "fact_returns",
    "fact_risk_metrics",
    "fact_correlations",
    "fact_regimes",
    "fact_regimes_v2",
)
VIEWS: tuple[str, ...] = ("price_adjustment_status", "prices_clean", "macro_asof")

# The series of macro_asof are the rows of macro_publication_lags, synced from
# config/macro_lags.yaml by mrm.storage.clean.prepare_clean_prices (ADR 0005).

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
    # Publication lags of the macro series (config/macro_lags.yaml, ADR 0005), synced in full
    # by prepare_clean_prices. macro_asof exposes exactly the series listed here.
    """
    CREATE TABLE IF NOT EXISTS macro_publication_lags (
        series_id VARCHAR   NOT NULL PRIMARY KEY,
        frequency VARCHAR   NOT NULL CHECK (frequency IN ('daily', 'weekly', 'monthly')),
        lag_days  INTEGER   NOT NULL CHECK (lag_days >= 0),
        synced_at TIMESTAMP NOT NULL  -- UTC
    )
    """,
    # Documented delayed first releases (date from ALFRED): replace obs_date + lag_days.
    """
    CREATE TABLE IF NOT EXISTS macro_release_overrides (
        series_id      VARCHAR   NOT NULL,
        obs_date       DATE      NOT NULL,
        available_date DATE      NOT NULL,
        reason         VARCHAR   NOT NULL,
        synced_at      TIMESTAMP NOT NULL,  -- UTC
        PRIMARY KEY (series_id, obs_date),
        CHECK (available_date > obs_date)
    )
    """,
    # Metric tables (Phase 2), local only: rebuilt by mrm.metrics.run, never exported as-is.
    # entity_id: ticker (asset), portfolio id (portfolio) or '<portfolio_id>:<ticker>' (holding).
    """
    CREATE TABLE IF NOT EXISTS fact_returns (
        entity_id     VARCHAR   NOT NULL,
        entity_type   VARCHAR   NOT NULL CHECK (entity_type IN ('asset', 'portfolio')),
        trade_date    DATE      NOT NULL,
        simple_return DOUBLE    NOT NULL,
        log_return    DOUBLE    NOT NULL,
        computed_at   TIMESTAMP NOT NULL,  -- UTC
        PRIMARY KEY (entity_id, trade_date)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS fact_risk_metrics (
        entity_id    VARCHAR   NOT NULL,
        entity_type  VARCHAR   NOT NULL CHECK (entity_type IN ('asset', 'portfolio', 'holding')),
        as_of_date   DATE      NOT NULL,
        metric       VARCHAR   NOT NULL,
        window_label VARCHAR   NOT NULL,
        value        DOUBLE    NOT NULL,
        n_obs        INTEGER   NOT NULL,
        computed_at  TIMESTAMP NOT NULL,  -- UTC
        PRIMARY KEY (entity_id, as_of_date, metric, window_label)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS fact_correlations (
        ticker_a     VARCHAR   NOT NULL,
        ticker_b     VARCHAR   NOT NULL,
        trade_date   DATE      NOT NULL,
        window_label VARCHAR   NOT NULL,
        correlation  DOUBLE    NOT NULL CHECK (correlation BETWEEN -1 AND 1),
        computed_at  TIMESTAMP NOT NULL,  -- UTC
        PRIMARY KEY (ticker_a, ticker_b, trade_date, window_label),
        CHECK (ticker_a < ticker_b)
    )
    """,
    # Rules-based regimes v1 (Phase 3.1, ADR 0006), rebuilt by mrm.regimes.run: one row per
    # trading day. Inputs are the point-in-time values of macro_asof. *_history: 'backcast' if
    # the value's available_date < the index's first ALFRED vintage (config/regime_rules.yaml),
    # else 'revised'; both are FRED's latest vintage (re-estimated), never the real-time value.
    # NULL regime / flag: the input has no as-of value yet (sahm_gap: warm-up of the rule).
    """
    CREATE TABLE IF NOT EXISTS fact_regimes (
        trade_date          DATE      PRIMARY KEY,
        regime              VARCHAR   CHECK (regime IN ('calm', 'tension', 'stress')),
        nfci_value          DOUBLE,
        nfci_value_date     DATE,
        nfci_available_date DATE,
        nfci_age_days       INTEGER,
        nfci_history        VARCHAR   CHECK (nfci_history IN ('backcast', 'revised')),
        stlfsi4_value       DOUBLE,
        stlfsi4_value_date  DATE,
        stlfsi4_regime      VARCHAR   CHECK (stlfsi4_regime IN ('calm', 'tension', 'stress')),
        stlfsi4_history     VARCHAR   CHECK (stlfsi4_history IN ('backcast', 'revised')),
        regime_agrees       BOOLEAN,
        t10y2y_value        DOUBLE,
        t10y2y_value_date   DATE,
        curve_inverted      BOOLEAN,
        unrate_value        DOUBLE,
        unrate_value_date   DATE,
        sahm_gap            DOUBLE,
        unemployment_rising BOOLEAN,
        computed_at         TIMESTAMP NOT NULL  -- UTC
    )
    """,
    # HMM regimes v2 (Phase 3.2, ADR 0007), rebuilt by mrm.regimes.run_v2: one row per trading
    # day per variant. 'point_in_time' = official (expanding z-scores, yearly refits on rows
    # before each refit, filtered probabilities); 'full_sample' = descriptive, smoothed, uses
    # the whole sample (is_point_in_time = false: never for performance or backtests). Row t is
    # known at the close of t: consumers act on it from t + 1. Model columns are NULL only in
    # the warm-up (window + zscore_min_obs - 1 + min_train_days rows for point_in_time).
    """
    CREATE TABLE IF NOT EXISTS fact_regimes_v2 (
        trade_date       DATE      NOT NULL,
        variant          VARCHAR   NOT NULL CHECK (variant IN ('point_in_time', 'full_sample')),
        is_point_in_time BOOLEAN   NOT NULL,
        regime           VARCHAR   CHECK (regime IN ('calm', 'tension', 'stress')),
        p_calm           DOUBLE    CHECK (p_calm BETWEEN 0 AND 1),
        p_tension        DOUBLE    CHECK (p_tension BETWEEN 0 AND 1),
        p_stress         DOUBLE    CHECK (p_stress BETWEEN 0 AND 1),
        spy_vol_60d      DOUBLE,
        nfci_value       DOUBLE,
        t10y2y_value     DOUBLE,
        z_spy_vol        DOUBLE,
        z_nfci           DOUBLE,
        z_t10y2y         DOUBLE,
        train_end_date   DATE,
        train_rows       INTEGER,
        computed_at      TIMESTAMP NOT NULL,  -- UTC
        PRIMARY KEY (trade_date, variant),
        CHECK (is_point_in_time = (variant = 'point_in_time'))
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
    # Point-in-time macro on the trading calendar of prices_clean, long format (ADR 0005).
    # A value becomes available on available_date = obs_date + lag_days (or its documented
    # override) and is used only on trading days strictly after it: available_date < trade_date.
    # Lag 0 (daily yields) is the Phase 1.6 rule obs_date < trade_date. available_date is made
    # non-decreasing in obs_date (running max: it can only delay a value), and among values
    # available on the same date the latest obs_date wins, so each trading day sees the most
    # recent reference period already published. Forward-fill only forward; NULL before a
    # series starts. is_filled: the value was already available on the previous trading day.
    """
    CREATE OR REPLACE VIEW macro_asof AS
    WITH calendar AS (
        SELECT trade_date, lag(trade_date) OVER (ORDER BY trade_date) AS prev_trade_date
        FROM (SELECT DISTINCT trade_date FROM prices_clean)
    ),
    grid AS (
        SELECT c.trade_date, c.prev_trade_date, l.series_id
        FROM calendar c CROSS JOIN macro_publication_lags l
    ),
    released AS (
        SELECT
            m.series_id,
            m.obs_date,
            m.value,
            coalesce(o.available_date, m.obs_date + l.lag_days) AS release_date
        FROM raw_macro m
        JOIN macro_publication_lags l USING (series_id)
        LEFT JOIN macro_release_overrides o USING (series_id, obs_date)
        WHERE m.value IS NOT NULL
    ),
    monotone AS (
        SELECT
            series_id,
            obs_date,
            value,
            max(release_date) OVER (
                PARTITION BY series_id ORDER BY obs_date ROWS UNBOUNDED PRECEDING
            ) AS available_date
        FROM released
    ),
    obs AS (
        SELECT
            series_id,
            available_date,
            max(obs_date) AS obs_date,
            arg_max(value, obs_date) AS value
        FROM monotone
        GROUP BY series_id, available_date
    )
    SELECT
        g.trade_date,
        g.series_id,
        o.value,
        o.obs_date AS value_date,
        coalesce(o.available_date < g.prev_trade_date, false) AS is_filled,
        o.available_date,
        CAST(g.trade_date - o.obs_date AS INTEGER) AS age_days
    FROM grid g
    ASOF LEFT JOIN obs o
        ON o.series_id = g.series_id AND g.trade_date > o.available_date
    """,
)


def create_schema(con: duckdb.DuckDBPyConnection) -> None:
    """Create all warehouse objects if they do not exist yet."""
    for statement in DDL:
        con.execute(statement)
