"""Read inputs from prices_clean / macro_asof and write the fact_* metric tables.

The only module of ``mrm.metrics`` that touches the database. Callers must run
``mrm.storage.clean.prepare_clean_prices(con)`` first. Never reads ``raw_*``.
"""

from datetime import datetime

import duckdb
import pandas as pd

from mrm.metrics.pipeline import MetricsResult

RISK_FREE_SERIES = "DGS3MO"


def load_close_panel(con: duckdb.DuckDBPyConnection) -> pd.DataFrame:
    """Wide close panel from ``prices_clean``: index = every trade_date in the view (union
    across tickers), one column per ticker, NaN where a ticker has no row that day."""
    long = con.execute("SELECT trade_date, ticker, close FROM prices_clean").df()
    panel = long.pivot(index="trade_date", columns="ticker", values="close").sort_index()
    panel.index = pd.DatetimeIndex(panel.index, name="trade_date")
    panel.columns.name = None
    return panel.sort_index(axis=1)


def load_risk_free(
    con: duckdb.DuckDBPyConnection, series_id: str = RISK_FREE_SERIES
) -> pd.DataFrame:
    """Point-in-time annual yield (percent) from ``macro_asof``: columns ``value`` (NaN before
    the series starts) and ``is_filled``, indexed by trade_date."""
    frame = con.execute(
        "SELECT trade_date, value, is_filled FROM macro_asof WHERE series_id = ? "
        "ORDER BY trade_date",
        [series_id],
    ).df()
    frame.index = pd.DatetimeIndex(frame.pop("trade_date"), name="trade_date")
    frame["value"] = frame["value"].astype(float)
    return frame


def write_metrics(
    con: duckdb.DuckDBPyConnection,
    result: MetricsResult,
    portfolio_id: str,
    computed_at: datetime,
) -> None:
    """Replace asset rows, this portfolio's rows and all correlations in one transaction.

    Rows of other portfolios (other ``portfolio_id`` / ``'<id>:*'`` holdings) are kept.
    """
    con.register("new_returns", result.returns)
    con.register("new_metrics", result.risk_metrics)
    con.register("new_corr", result.correlations)
    con.execute("BEGIN TRANSACTION")
    try:
        con.execute(
            "DELETE FROM fact_returns WHERE entity_type = 'asset' OR entity_id = ?",
            [portfolio_id],
        )
        con.execute(
            "DELETE FROM fact_risk_metrics WHERE entity_type = 'asset' OR entity_id = ? "
            "OR (entity_type = 'holding' AND starts_with(entity_id, ?))",
            [portfolio_id, f"{portfolio_id}:"],
        )
        con.execute("DELETE FROM fact_correlations")
        con.execute(
            "INSERT INTO fact_returns SELECT entity_id, entity_type, CAST(trade_date AS DATE), "
            "simple_return, log_return, ? FROM new_returns",
            [computed_at],
        )
        con.execute(
            "INSERT INTO fact_risk_metrics SELECT entity_id, entity_type, "
            "CAST(as_of_date AS DATE), metric, window_label, value, n_obs, ? FROM new_metrics",
            [computed_at],
        )
        con.execute(
            "INSERT INTO fact_correlations SELECT ticker_a, ticker_b, CAST(trade_date AS DATE), "
            "window_label, correlation, ? FROM new_corr",
            [computed_at],
        )
        con.execute("COMMIT")
    except Exception:
        con.execute("ROLLBACK")
        raise
    finally:
        for name in ("new_returns", "new_metrics", "new_corr"):
            con.unregister(name)


def table_summary(con: duckdb.DuckDBPyConnection) -> list[tuple[str, int, int, object, object]]:
    """(table, rows, distinct entities, min date, max date) for each fact table. No values."""
    queries = {
        "fact_returns": "SELECT count(*), count(DISTINCT entity_id), min(trade_date), "
        "max(trade_date) FROM fact_returns",
        "fact_risk_metrics": "SELECT count(*), count(DISTINCT entity_id), min(as_of_date), "
        "max(as_of_date) FROM fact_risk_metrics",
        "fact_correlations": "SELECT count(*), count(DISTINCT (ticker_a, ticker_b)), "
        "min(trade_date), max(trade_date) FROM fact_correlations",
    }
    return [(name, *con.execute(sql).fetchone()) for name, sql in queries.items()]
