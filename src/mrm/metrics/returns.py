"""Daily returns, fixed-weight portfolio returns and the daily risk-free rate.

Pure functions on pandas objects; no database access. Inputs are wide panels indexed by
``trade_date`` (ascending) with one column per ticker, on the full trading calendar: a NaN
close means the ticker has no quote that day. Nothing is imputed (see docs/methodology.md).
"""

import numpy as np
import pandas as pd

TRADING_DAYS = 252


def _check_prices(prices: pd.DataFrame) -> None:
    if not prices.index.is_monotonic_increasing or not prices.index.is_unique:
        raise ValueError("prices index must be unique and ascending")
    if (prices <= 0).any().any():
        raise ValueError("prices must be > 0")


def simple_returns(prices: pd.DataFrame) -> pd.DataFrame:
    """``r_t = P_t / P_{t-1} - 1`` between consecutive calendar rows.

    NaN when the close at ``t`` or at the previous calendar day is missing, so a return
    never spans a gap. The first row is always NaN.
    """
    _check_prices(prices)
    return prices / prices.shift(1) - 1.0


def log_returns(prices: pd.DataFrame) -> pd.DataFrame:
    """``ln(P_t / P_{t-1})`` with the same NaN rules as :func:`simple_returns`."""
    _check_prices(prices)
    return np.log(prices / prices.shift(1))


def portfolio_returns(returns: pd.DataFrame, weights: dict[str, float]) -> pd.Series:
    """Daily-rebalanced fixed-weight portfolio: ``r_p,t = sum_i w_i r_i,t``.

    NaN on any day where a holding's return is NaN (weights are never renormalised).
    Raises ValueError when a holding has no column in ``returns``.
    """
    missing = sorted(set(weights) - set(returns.columns))
    if missing:
        raise ValueError(f"no returns for portfolio holdings: {', '.join(missing)}")
    tickers = list(weights)
    w = pd.Series(weights, dtype=float)[tickers]
    sub = returns[tickers]
    result = sub.mul(w, axis=1).sum(axis=1, min_count=len(tickers))
    result[sub.isna().any(axis=1)] = np.nan
    return result.rename(None)


def daily_risk_free(annual_pct: pd.Series, periods: int = TRADING_DAYS) -> pd.Series:
    """Convert an annual yield in percent (DGS3MO) to a daily rate: ``(1 + y/100)^(1/252) - 1``.

    NaN stays NaN (no fill backwards, no zero).
    """
    return (1.0 + annual_pct.astype(float) / 100.0) ** (1.0 / periods) - 1.0


def excess_returns(returns: pd.Series, risk_free: pd.Series) -> pd.Series:
    """``r_t - rf_t`` aligned on dates; NaN where either side is NaN or missing."""
    return returns - risk_free.reindex(returns.index)
