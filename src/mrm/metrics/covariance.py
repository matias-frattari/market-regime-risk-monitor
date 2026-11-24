"""Rolling pairwise correlation and Euler risk contribution.

Pure functions; no database access. Each rolling value at ``t`` uses the ``window`` returns
ending at ``t`` and only when the window is complete with no NaN for the assets involved.
"""

import math

import numpy as np
import pandas as pd

from mrm.metrics.returns import TRADING_DAYS

CORRELATION_COLUMNS = ["trade_date", "ticker_a", "ticker_b", "correlation"]
CONTRIBUTION_COLUMNS = ["trade_date", "ticker", "risk_contrib", "risk_contrib_pct"]


def _windows(values: np.ndarray, window: int) -> np.ndarray:
    """Trailing windows, shape ``(n - window + 1, window, k)``; row i ends at ``i + window - 1``."""
    return np.lib.stride_tricks.sliding_window_view(values, window, axis=0).transpose(0, 2, 1)


def rolling_correlations(returns: pd.DataFrame, window: int = 126) -> pd.DataFrame:
    """Pearson correlation of each pair ``ticker_a < ticker_b`` over the trailing window.

    Long format (``CORRELATION_COLUMNS``). Rows are emitted only for complete windows where
    both series have no NaN and non-zero dispersion.
    """
    tickers = sorted(returns.columns)
    if len(returns) < window or len(tickers) < 2:
        return pd.DataFrame(columns=CORRELATION_COLUMNS)
    values = returns[tickers].to_numpy(dtype=float)
    win = _windows(values, window)  # (m, window, k)
    end_dates = returns.index[window - 1 :]
    valid = ~np.isnan(win).any(axis=1)  # (m, k)
    centred = win - win.mean(axis=1, keepdims=True)
    sd = np.sqrt((centred**2).sum(axis=1))  # (m, k), unnormalised
    frames: list[pd.DataFrame] = []
    for i, a in enumerate(tickers):
        for j in range(i + 1, len(tickers)):
            denom = sd[:, i] * sd[:, j]
            ok = valid[:, i] & valid[:, j] & (denom > 0)
            corr = (centred[:, :, i] * centred[:, :, j]).sum(axis=1)[ok] / denom[ok]
            frames.append(
                pd.DataFrame(
                    {
                        "trade_date": end_dates[ok],
                        "ticker_a": a,
                        "ticker_b": tickers[j],
                        "correlation": np.clip(corr, -1.0, 1.0),
                    }
                )
            )
    return pd.concat(frames, ignore_index=True)


def euler_contributions(cov: np.ndarray, weights: np.ndarray) -> tuple[np.ndarray, float]:
    """Euler decomposition of portfolio volatility.

    ``sigma_p = sqrt(w' S w)``, ``RC_i = w_i (S w)_i / sigma_p``; ``sum(RC) = sigma_p``.
    Returns ``(RC, sigma_p)``; RC is all NaN when ``sigma_p`` is 0.
    """
    w = np.asarray(weights, dtype=float)
    marginal = np.asarray(cov, dtype=float) @ w
    sigma = math.sqrt(max(float(w @ marginal), 0.0))
    if sigma == 0.0:
        return np.full_like(w, np.nan), 0.0
    return w * marginal / sigma, sigma


def rolling_euler_contributions(
    returns: pd.DataFrame,
    weights: dict[str, float],
    window: int = 126,
    periods: int = TRADING_DAYS,
) -> pd.DataFrame:
    """Annualized Euler risk contribution per holding on each date, from the sample
    covariance (ddof=1) of the trailing ``window`` returns.

    Long format (``CONTRIBUTION_COLUMNS``): ``risk_contrib`` in annualized volatility units
    (they sum to the portfolio's annualized volatility on that date), ``risk_contrib_pct`` =
    ``RC_i / sigma_p`` (sums to 1). Dates with an incomplete window are skipped.
    """
    tickers = list(weights)
    missing = sorted(set(tickers) - set(returns.columns))
    if missing:
        raise ValueError(f"no returns for portfolio holdings: {', '.join(missing)}")
    if len(returns) < window:
        return pd.DataFrame(columns=CONTRIBUTION_COLUMNS)
    w = np.array([weights[t] for t in tickers], dtype=float)
    win = _windows(returns[tickers].to_numpy(dtype=float), window)
    end_dates = returns.index[window - 1 :]
    rows: list[tuple[object, str, float, float]] = []
    for date, block in zip(end_dates, win, strict=True):
        if np.isnan(block).any():
            continue
        cov = np.atleast_2d(np.cov(block, rowvar=False, ddof=1))  # 1 holding -> 1x1
        rc, sigma = euler_contributions(cov, w)
        if sigma == 0.0:
            continue
        scale = math.sqrt(periods)
        rows.extend(
            (date, t, float(c * scale), float(c / sigma)) for t, c in zip(tickers, rc, strict=True)
        )
    return pd.DataFrame(rows, columns=CONTRIBUTION_COLUMNS)
