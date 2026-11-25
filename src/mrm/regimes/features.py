"""Point-in-time features of the HMM regimes v2 (ADR 0007): pure functions, no database.

The row of trading day ``t`` uses SPY closes up to and including ``t`` (60-day realised
volatility, computed with the same function as ``fact_risk_metrics``) and the ``macro_asof``
values of NFCI and T10Y2Y (published before ``t``). It is therefore known at the close of ``t``.
"""

from collections.abc import Mapping

import numpy as np
import pandas as pd

from mrm.metrics.returns import simple_returns
from mrm.metrics.risk import ZERO_DISPERSION, rolling_volatility

FEATURES: tuple[str, ...] = ("spy_vol_60d", "nfci", "t10y2y")
Z_COLUMNS: tuple[str, ...] = ("z_spy_vol", "z_nfci", "z_t10y2y")
VOL_FEATURE = "spy_vol_60d"


def build_features(
    spy_close: pd.Series, macro: Mapping[str, pd.Series], window: int
) -> pd.DataFrame:
    """Raw features on the trading-day index of ``spy_close`` (columns ``FEATURES``).

    ``spy_close``: SPY close per trading day, NaN where SPY has no quote (no imputation: a
    missing close makes every window that spans it NaN). ``macro``: as-of values of ``NFCI``
    and ``T10Y2Y`` indexed by trade_date; they are aligned on ``spy_close.index``.
    """
    returns = simple_returns(spy_close.to_frame("SPY"))["SPY"]
    index = spy_close.index
    return pd.DataFrame(
        {
            "spy_vol_60d": rolling_volatility(returns, window),
            "nfci": macro["NFCI"].reindex(index).astype(float),
            "t10y2y": macro["T10Y2Y"].reindex(index).astype(float),
        },
        index=index,
    )[list(FEATURES)]


def _zscore(values: pd.DataFrame, mean: pd.DataFrame, std: pd.DataFrame) -> pd.DataFrame:
    z = (values - mean) / std.where(std > ZERO_DISPERSION)
    z.columns = list(Z_COLUMNS)
    return z


def expanding_zscore(features: pd.DataFrame, min_obs: int) -> pd.DataFrame:
    """``(x_t - mean(x_1..t)) / std(x_1..t, ddof=1)`` per feature (columns ``Z_COLUMNS``).

    Means and standard deviations use only the non-missing values up to and including ``t``.
    The z-score is NaN while a feature has fewer than ``min_obs`` values, where ``x_t`` is
    missing, and where the expanding std is zero.
    """
    expanding = features.expanding(min_periods=min_obs)
    return _zscore(features, expanding.mean(), expanding.std(ddof=1))


def full_sample_zscore(features: pd.DataFrame) -> pd.DataFrame:
    """z-scores with the mean and std (ddof=1) of the rows where every feature is known.

    **Uses the whole sample: not point-in-time** (descriptive full-sample version only).
    """
    complete = features.dropna()
    mean = pd.DataFrame(
        np.broadcast_to(complete.mean().to_numpy(), features.shape),
        index=features.index,
        columns=features.columns,
    )
    std = pd.DataFrame(
        np.broadcast_to(complete.std(ddof=1).to_numpy(), features.shape),
        index=features.index,
        columns=features.columns,
    )
    return _zscore(features, mean, std)
