# SYNTHETIC — not real market data: seeded random closes and index values on a made-up
# business-day calendar, only to test the feature functions.
import numpy as np
import pandas as pd
import pytest

from mrm.metrics.returns import simple_returns
from mrm.metrics.risk import rolling_volatility
from mrm.regimes.features import (
    FEATURES,
    Z_COLUMNS,
    build_features,
    expanding_zscore,
    full_sample_zscore,
)

INDEX = pd.bdate_range("2020-01-01", periods=300, name="trade_date")


def _synthetic(seed: int = 3) -> tuple[pd.Series, dict[str, pd.Series]]:
    rng = np.random.default_rng(seed)
    close = pd.Series(100 * np.exp(np.cumsum(rng.normal(0, 0.01, len(INDEX)))), index=INDEX)
    macro = {
        "NFCI": pd.Series(np.cumsum(rng.normal(0, 0.05, len(INDEX))), index=INDEX),
        "T10Y2Y": pd.Series(np.cumsum(rng.normal(0, 0.02, len(INDEX))), index=INDEX),
    }
    return close, macro


def test_vol_feature_reuses_the_metrics_function() -> None:
    close, macro = _synthetic()
    features = build_features(close, macro, window=60)
    expected = rolling_volatility(simple_returns(close.to_frame("SPY"))["SPY"], 60)
    pd.testing.assert_series_equal(features["spy_vol_60d"], expected, check_names=False)
    assert list(features.columns) == list(FEATURES)
    assert features["spy_vol_60d"].isna().sum() == 60  # first return row + 59 incomplete windows


def test_missing_close_breaks_windows_without_imputation() -> None:
    close, macro = _synthetic()
    close.iloc[150] = np.nan
    vol = build_features(close, macro, window=60)["spy_vol_60d"]
    # returns at 150 and 151 are NaN, so every window ending in 150..210 is NaN
    assert vol.iloc[150:211].isna().all()
    assert vol.iloc[149] == vol.iloc[149] and vol.iloc[211] == vol.iloc[211]


def test_macro_aligned_on_the_price_calendar() -> None:
    close, macro = _synthetic()
    macro["NFCI"] = macro["NFCI"].iloc[10:]
    features = build_features(close, macro, window=60)
    assert features.index.equals(INDEX)
    assert features["nfci"].iloc[:10].isna().all()


def test_expanding_zscore_min_obs_and_values() -> None:
    close, macro = _synthetic()
    features = build_features(close, macro, window=60)
    z = expanding_zscore(features, min_obs=60)
    assert list(z.columns) == list(Z_COLUMNS)
    # z known from the 60th value of each feature, the current one included
    assert z["z_nfci"].isna().sum() == 59
    assert z["z_spy_vol"].isna().sum() == 60 + 59
    t = 200
    x = features["t10y2y"].iloc[: t + 1]
    assert z["z_t10y2y"].iloc[t] == pytest.approx((x.iloc[-1] - x.mean()) / x.std(ddof=1))


def test_expanding_zscore_ignores_future_rows() -> None:
    close, macro = _synthetic()
    features = build_features(close, macro, window=60)
    full = expanding_zscore(features, min_obs=60)
    future = features.copy()
    future.iloc[200:] = future.iloc[200:] * 5 + 3
    head = expanding_zscore(features.iloc[:200], min_obs=60)
    pd.testing.assert_frame_equal(expanding_zscore(future, 60).iloc[:200], full.iloc[:200])
    pd.testing.assert_frame_equal(head, full.iloc[:200])


def test_zero_dispersion_gives_nan() -> None:
    features = pd.DataFrame({c: 1.0 for c in FEATURES}, index=INDEX)
    assert expanding_zscore(features, 5).isna().all().all()


def test_full_sample_zscore_uses_complete_rows() -> None:
    close, macro = _synthetic()
    features = build_features(close, macro, window=60)
    z = full_sample_zscore(features)
    complete = z.dropna()
    assert len(complete) == len(INDEX) - 60
    assert np.allclose(complete.mean(), 0.0) and np.allclose(complete.std(ddof=1), 1.0)
