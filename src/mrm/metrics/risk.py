"""Volatility, drawdown, Sharpe ratio and VaR/CVaR on daily simple returns.

Pure functions; no database access. Full-period functions drop NaN returns first and return
NaN when there are too few observations. Rolling functions require a complete window
(``min_periods = window``): any NaN inside the window yields NaN, and the value at ``t``
uses only returns up to ``t``. VaR/CVaR are one-day losses reported as positive numbers.
"""

import math
from statistics import NormalDist

import numpy as np
import pandas as pd

from mrm.metrics.returns import TRADING_DAYS

CONFIDENCE = 0.95
# Daily std below this is floating-point noise of a constant series, treated as zero.
ZERO_DISPERSION = 1e-12


def annualized_volatility(returns: pd.Series, periods: int = TRADING_DAYS) -> float:
    """``std(r, ddof=1) * sqrt(periods)``; NaN with fewer than 2 observations."""
    r = returns.dropna()
    if len(r) < 2:
        return math.nan
    return float(r.std(ddof=1) * math.sqrt(periods))


def rolling_volatility(
    returns: pd.Series, window: int = 60, periods: int = TRADING_DAYS
) -> pd.Series:
    """Annualized volatility over the trailing ``window`` returns ending at each date."""
    return returns.rolling(window, min_periods=window).std(ddof=1) * math.sqrt(periods)


def annualized_return(returns: pd.Series, periods: int = TRADING_DAYS) -> float:
    """Geometric annualized return ``exp(mean(ln(1 + r)) * periods) - 1``."""
    r = returns.dropna()
    if r.empty:
        return math.nan
    return float(math.exp(np.log1p(r).mean() * periods) - 1.0)


def drawdown(returns: pd.Series) -> pd.Series:
    """Drawdown from the running peak of the wealth index ``W_t = prod(1 + r)``, ``W_0 = 1``.

    ``DD_t = W_t / max(1, max_{s<=t} W_s) - 1`` (<= 0). NaN returns are skipped and stay NaN.
    """
    r = returns.dropna()
    wealth = (1.0 + r).cumprod()
    peak = wealth.cummax().clip(lower=1.0)
    return (wealth / peak - 1.0).reindex(returns.index)


def max_drawdown(returns: pd.Series) -> float:
    """Most negative drawdown over the period (e.g. -0.25 for a 25% peak-to-trough fall)."""
    dd = drawdown(returns).dropna()
    return float(dd.min()) if not dd.empty else math.nan


def sharpe_ratio(excess: pd.Series, periods: int = TRADING_DAYS) -> float:
    """Annualized Sharpe ``mean(r - rf) / std(r - rf, ddof=1) * sqrt(periods)``.

    NaN with fewer than 2 observations or zero dispersion (std <= ``ZERO_DISPERSION``).
    """
    x = excess.dropna()
    if len(x) < 2:
        return math.nan
    sd = x.std(ddof=1)
    if not sd > ZERO_DISPERSION:
        return math.nan
    return float(x.mean() / sd * math.sqrt(periods))


def historical_var_cvar(returns: pd.Series, confidence: float = CONFIDENCE) -> tuple[float, float]:
    """Historical one-day VaR and CVaR as positive losses.

    ``q = quantile(r, 1 - confidence)`` with linear interpolation between order statistics
    (numpy ``method="linear"``); ``VaR = -q``; ``CVaR = -mean(r | r <= q)``.
    """
    r = returns.dropna().to_numpy(dtype=float)
    if r.size == 0:
        return math.nan, math.nan
    q = float(np.quantile(r, 1.0 - confidence, method="linear"))
    return -q, -float(r[r <= q].mean())


def parametric_var_cvar(returns: pd.Series, confidence: float = CONFIDENCE) -> tuple[float, float]:
    """Gaussian one-day VaR and CVaR as positive losses, from the sample mean and std (ddof=1).

    ``z = N^-1(confidence)``; ``VaR = -(mu - z sigma)``; ``CVaR = -(mu - sigma phi(z) / (1 - c))``.
    """
    r = returns.dropna()
    if len(r) < 2:
        return math.nan, math.nan
    mu, sigma = float(r.mean()), float(r.std(ddof=1))
    normal = NormalDist()
    z = normal.inv_cdf(confidence)
    var = -(mu - z * sigma)
    cvar = -(mu - sigma * normal.pdf(z) / (1.0 - confidence))
    return var, cvar
