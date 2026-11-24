"""Compute the Phase 2 risk metrics and write fact_returns, fact_risk_metrics and
fact_correlations.

CLI::

    uv run mrm metrics [--portfolio PATH] [--dry-run]

Reads ``prices_clean`` (the clean layer is prepared first; rows on dates that are not NYSE
trading days are discarded and counted) and DGS3MO from ``macro_asof``.
Prints only counts and date ranges: never prices or per-ticker returns.

Exit codes: 0 success, 1 no usable data, 2 config error.
"""

import argparse
import sys
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

import duckdb
import pandas as pd

from mrm.config import (
    default_portfolio_path,
    load_data_quality_config,
    load_portfolio,
    load_price_adjustments,
)
from mrm.metrics.io import (
    RISK_FREE_SERIES,
    load_close_panel,
    load_risk_free,
    table_summary,
    write_metrics,
)
from mrm.metrics.pipeline import MetricsResult, compute_metrics
from mrm.storage.clean import prepare_clean_prices
from mrm.storage.db import connect
from mrm.storage.models import Portfolio, PriceAdjustment


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="mrm metrics",
        description="Risk metrics from prices_clean and macro_asof into the fact_* tables.",
    )
    parser.add_argument(
        "--portfolio",
        type=Path,
        default=None,
        help="Portfolio YAML (default: config/portfolio.yaml, else portfolio.example.yaml)",
    )
    parser.add_argument("--dry-run", action="store_true", help="Compute but write nothing")
    return parser.parse_args(argv)


def _fmt_range(lo: object, hi: object) -> str:
    def day(value: object) -> str:
        return "-" if value is None or pd.isna(value) else str(pd.Timestamp(value).date())

    return f"{day(lo)} .. {day(hi)}"


def _frame_summary(result: MetricsResult) -> list[tuple[str, int, int, object, object]]:
    r, m, c = result.returns, result.risk_metrics, result.correlations
    return [
        ("fact_returns", len(r), r.entity_id.nunique(), r.trade_date.min(), r.trade_date.max()),
        ("fact_risk_metrics", len(m), m.entity_id.nunique(), m.as_of_date.min(),
         m.as_of_date.max()),
        ("fact_correlations", len(c), len(c[["ticker_a", "ticker_b"]].drop_duplicates()),
         c.trade_date.min(), c.trade_date.max()),
    ]  # fmt: skip


def _risk_free_lines(rf: pd.DataFrame, return_days: pd.Index) -> list[str]:
    on_days = rf.reindex(return_days)
    n_null = int(on_days["value"].isna().sum())
    n_filled = int(on_days["is_filled"].fillna(False).astype(bool).sum())
    return [
        f"risk-free {RISK_FREE_SERIES} on {len(return_days)} portfolio return days: "
        f"{n_null} NULL (excluded from Sharpe), {n_filled} forward-filled (carried value used)"
    ]


def main(
    argv: Sequence[str] | None = None,
    *,
    con: duckdb.DuckDBPyConnection | None = None,
    portfolio: Portfolio | None = None,
    adjustments: Sequence[PriceAdjustment] | None = None,
) -> int:
    """CLI entry point. ``con``/``portfolio``/``adjustments`` are injectable for tests."""
    args = _parse_args(argv)
    try:
        portfolio = portfolio or load_portfolio(args.portfolio or default_portfolio_path())
        adjustments = load_price_adjustments() if adjustments is None else adjustments
        closures = load_data_quality_config().special_closures
    except (OSError, ValueError) as exc:
        print(f"error: invalid config: {exc}")
        return 2

    own_con = con is None
    con = con or connect()
    try:
        prepare_clean_prices(con, adjustments=adjustments)
        prices, n_off_calendar = load_close_panel(con, closures)
        if prices.empty:
            print("error: prices_clean is empty; run `mrm ingest prices` first")
            return 1
        missing = sorted(set(portfolio.weights) - set(prices.columns))
        if missing:
            print(f"error: portfolio holdings without prices: {', '.join(missing)}")
            return 1
        rf = load_risk_free(con)
        result = compute_metrics(prices, rf["value"], portfolio)
        if not args.dry_run:
            write_metrics(
                con, result, portfolio.portfolio_id, datetime.now(UTC).replace(tzinfo=None)
            )
            summary = table_summary(con)
        else:
            summary = _frame_summary(result)
    finally:
        if own_con:
            con.close()

    weights = ", ".join(f"{t} {w:.2f}" for t, w in portfolio.weights.items())
    port_days = result.returns.loc[result.returns.entity_type == "portfolio", "trade_date"]
    print(f"Portfolio {portfolio.portfolio_id}: {weights}")
    print(
        f"prices_clean: {prices.shape[1]} tickers, {len(prices)} trading days, "
        f"{_fmt_range(prices.index.min(), prices.index.max())}"
    )
    print(f"off-calendar rows discarded (not NYSE trading days): {n_off_calendar}")
    for line in _risk_free_lines(rf, pd.DatetimeIndex(port_days)):
        print(line)
    print(f"{'table':<20}{'rows':>10}{'entities':>10}  date range")
    for name, rows, entities, lo, hi in summary:
        print(f"{name:<20}{rows:>10}{entities:>10}  {_fmt_range(lo, hi)}")
    print("dry run: nothing written" if args.dry_run else "written (full rebuild, one transaction)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
