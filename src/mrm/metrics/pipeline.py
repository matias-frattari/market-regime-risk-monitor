"""Assemble the long-format rows of fact_returns, fact_risk_metrics and fact_correlations.

Pure: takes in-memory panels, returns DataFrames; reading and writing live in
``mrm.metrics.io``. See docs/methodology.md for formulas and NaN rules.
"""

from dataclasses import dataclass

import numpy as np
import pandas as pd

from mrm.metrics.covariance import rolling_correlations, rolling_euler_contributions
from mrm.metrics.returns import (
    daily_risk_free,
    excess_returns,
    log_returns,
    portfolio_returns,
    simple_returns,
)
from mrm.metrics.risk import (
    annualized_return,
    annualized_return_from_prices,
    annualized_volatility,
    drawdown,
    historical_var_cvar,
    max_drawdown,
    parametric_var_cvar,
    price_drawdown,
    return_intervals,
    rolling_volatility,
    sharpe_ratio,
)
from mrm.storage.models import Portfolio

VOL_WINDOW = 60
CORR_WINDOW = 126
FULL = "full"
EXPANDING = "expanding"

RETURN_COLUMNS = ["entity_id", "entity_type", "trade_date", "simple_return", "log_return"]
METRIC_COLUMNS = [
    "entity_id", "entity_type", "as_of_date", "metric", "window_label", "value", "n_obs",
]  # fmt: skip
CORRELATION_FACT_COLUMNS = ["ticker_a", "ticker_b", "trade_date", "window_label", "correlation"]


@dataclass(frozen=True, slots=True)
class MetricsResult:
    """Rows ready to write (no ``computed_at``; the writer stamps it)."""

    returns: pd.DataFrame
    risk_metrics: pd.DataFrame
    correlations: pd.DataFrame


def holding_entity_id(portfolio_id: str, ticker: str) -> str:
    """Entity id of a holding row (Euler contribution): ``'<portfolio_id>:<ticker>'``."""
    return f"{portfolio_id}:{ticker}"


def _returns_rows(
    simple: pd.Series, log: pd.Series, entity_id: str, entity_type: str
) -> pd.DataFrame:
    frame = pd.DataFrame({"simple_return": simple, "log_return": log}).dropna()
    frame = frame.rename_axis("trade_date").reset_index()
    frame.insert(0, "entity_type", entity_type)
    frame.insert(0, "entity_id", entity_id)
    return frame[RETURN_COLUMNS]


@dataclass(frozen=True, slots=True)
class _Level:
    """Level-based metrics: from the close for assets, from the returns for the portfolio."""

    drawdown: pd.Series
    dd_n_obs: pd.Series
    ann_return: float
    max_drawdown: float
    n_intervals: int


def _asset_level(close: pd.Series) -> _Level:
    """From the close level, so a move across a missing quote is kept (see methodology.md)."""
    dd = price_drawdown(close)
    intervals = return_intervals(close)
    return _Level(
        drawdown=dd,
        dd_n_obs=intervals.reindex(close.index),
        ann_return=annualized_return_from_prices(close),
        max_drawdown=float(dd.min()) if dd.notna().any() else np.nan,
        n_intervals=int(intervals.iloc[-1]) if not intervals.empty else 0,
    )


def _portfolio_level(r: pd.Series) -> _Level:
    """From the returns: a day where any holding has no return is skipped (limitation)."""
    n = int(r.notna().sum())
    return _Level(
        drawdown=drawdown(r),
        dd_n_obs=r.notna().cumsum(),
        ann_return=annualized_return(r),
        max_drawdown=max_drawdown(r),
        n_intervals=n,
    )


def _full_period_rows(
    r: pd.Series, rf_daily: pd.Series, entity_id: str, entity_type: str, level: _Level
) -> list[tuple]:
    observed = r.dropna()
    if observed.empty:
        return []
    as_of = observed.index[-1]
    excess = excess_returns(r, rf_daily).dropna()
    var_h, cvar_h = historical_var_cvar(r)
    var_p, cvar_p = parametric_var_cvar(r)
    n = len(observed)
    values = [
        ("ann_return", level.ann_return, level.n_intervals),
        ("ann_vol", annualized_volatility(r), n),
        ("max_drawdown", level.max_drawdown, level.n_intervals),
        ("sharpe", sharpe_ratio(excess), len(excess)),
        ("var_hist_95", var_h, n),
        ("cvar_hist_95", cvar_h, n),
        ("var_param_95", var_p, n),
        ("cvar_param_95", cvar_p, n),
    ]
    return [(entity_id, entity_type, as_of, m, FULL, v, k) for m, v, k in values if np.isfinite(v)]


def _series_rows(
    values: pd.Series, n_obs: pd.Series | int, entity_id: str, entity_type: str, metric: str,
    window_label: str,
) -> pd.DataFrame:  # fmt: skip
    frame = pd.DataFrame({"value": values, "n_obs": n_obs}).dropna(subset=["value"])
    frame = frame.rename_axis("as_of_date").reset_index()
    frame["entity_id"], frame["entity_type"] = entity_id, entity_type
    frame["metric"], frame["window_label"] = metric, window_label
    return frame[METRIC_COLUMNS]


def _entity_metrics(
    r: pd.Series,
    rf_daily: pd.Series,
    entity_id: str,
    entity_type: str,
    vol_window: int,
    level: _Level,
) -> list[pd.DataFrame]:
    full = pd.DataFrame(
        _full_period_rows(r, rf_daily, entity_id, entity_type, level), columns=METRIC_COLUMNS
    )
    vol = _series_rows(
        rolling_volatility(r, vol_window), vol_window, entity_id, entity_type,
        f"vol_{vol_window}d", f"{vol_window}d",
    )  # fmt: skip
    dd = _series_rows(level.drawdown, level.dd_n_obs, entity_id, entity_type, "drawdown", EXPANDING)
    return [full, vol, dd]


def compute_metrics(
    prices: pd.DataFrame,
    rf_annual_pct: pd.Series,
    portfolio: Portfolio,
    *,
    vol_window: int = VOL_WINDOW,
    corr_window: int = CORR_WINDOW,
) -> MetricsResult:
    """Compute every Phase 2 metric for each asset column of ``prices`` and the portfolio.

    ``prices``: wide close panel on the trading calendar (NaN = no quote).
    ``rf_annual_pct``: DGS3MO in percent indexed by trade_date (NaN before the series starts).
    """
    simple = simple_returns(prices)
    log = log_returns(prices)
    rf_daily = daily_risk_free(rf_annual_pct).reindex(simple.index)
    port = portfolio_returns(simple, portfolio.weights)
    pid = portfolio.portfolio_id

    return_frames = [_returns_rows(simple[t], log[t], t, "asset") for t in simple.columns]
    return_frames.append(_returns_rows(port, np.log1p(port), pid, "portfolio"))

    metric_frames: list[pd.DataFrame] = []
    for ticker in simple.columns:
        metric_frames += _entity_metrics(
            simple[ticker], rf_daily, ticker, "asset", vol_window, _asset_level(prices[ticker])
        )
    metric_frames += _entity_metrics(
        port, rf_daily, pid, "portfolio", vol_window, _portfolio_level(port)
    )

    euler = rolling_euler_contributions(simple, portfolio.weights, window=corr_window)
    label = f"{corr_window}d"
    for metric in ("risk_contrib", "risk_contrib_pct"):
        rows = pd.DataFrame(
            {
                "entity_id": [holding_entity_id(pid, t) for t in euler["ticker"]],
                "entity_type": "holding",
                "as_of_date": euler["trade_date"],
                "metric": metric,
                "window_label": label,
                "value": euler[metric],
                "n_obs": corr_window,
            }
        )
        metric_frames.append(rows[METRIC_COLUMNS])

    corr = rolling_correlations(simple, window=corr_window)
    corr["window_label"] = label

    return MetricsResult(
        returns=_concat(return_frames, RETURN_COLUMNS),
        risk_metrics=_concat(metric_frames, METRIC_COLUMNS, cast_int="n_obs"),
        correlations=corr[CORRELATION_FACT_COLUMNS].reset_index(drop=True),
    )


def _concat(frames: list[pd.DataFrame], columns: list[str], cast_int: str = "") -> pd.DataFrame:
    non_empty = [f for f in frames if not f.empty]
    out = pd.concat(non_empty, ignore_index=True) if non_empty else pd.DataFrame(columns=columns)
    if cast_int:
        out[cast_int] = out[cast_int].astype("int64")
    return out[columns]
