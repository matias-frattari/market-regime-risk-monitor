"""Rolling correlation and Euler risk contribution on SYNTHETIC returns."""

import math

import numpy as np
import pandas as pd
import pytest

from mrm.metrics.covariance import (
    euler_contributions,
    rolling_correlations,
    rolling_euler_contributions,
)
from mrm.metrics.returns import portfolio_returns
from mrm.metrics.risk import rolling_volatility


def _panel(n: int = 300, seed: int = 0) -> pd.DataFrame:
    rng = np.random.default_rng(seed)  # synthetic noise, not market data
    a = rng.normal(0, 0.01, n)
    return pd.DataFrame(
        {"A": a, "B": -2 * a, "C": rng.normal(0, 0.02, n), "D": a + rng.normal(0, 0.01, n)},
        index=pd.date_range("2020-01-01", periods=n, freq="B"),
    )


def _corr(result: pd.DataFrame, a: str, b: str) -> pd.Series:
    rows = result[(result.ticker_a == a) & (result.ticker_b == b)]
    return rows.set_index("trade_date")["correlation"]


def test_identical_and_opposite_series_correlate_plus_and_minus_one() -> None:
    panel = _panel()
    panel["E"] = panel["A"]
    result = rolling_correlations(panel, window=126)
    assert _corr(result, "A", "E").to_numpy() == pytest.approx(1.0)
    assert _corr(result, "A", "B").to_numpy() == pytest.approx(-1.0)
    assert len(_corr(result, "A", "C")) == len(panel) - 125
    assert set(zip(result.ticker_a, result.ticker_b, strict=True)) == {
        (a, b) for a in "ABCDE" for b in "ABCDE" if a < b
    }


def test_rolling_correlation_matches_pandas_and_ignores_the_future() -> None:
    panel = _panel()
    result = _corr(rolling_correlations(panel, window=126), "A", "D")
    t = panel.index[200]
    expected = panel["A"].iloc[75:201].corr(panel["D"].iloc[75:201])
    assert result[t] == pytest.approx(expected)
    changed = panel.copy()
    changed.iloc[201:] = 0.3
    assert _corr(rolling_correlations(changed, window=126), "A", "D")[t] == pytest.approx(
        result[t], abs=0, rel=0
    )


def test_rolling_correlation_skips_windows_with_nan() -> None:
    panel = _panel()
    panel.iloc[150, panel.columns.get_loc("C")] = np.nan
    ac = _corr(rolling_correlations(panel, window=126), "A", "C")
    gap = panel.index[150:276]
    assert not ac.index.isin(gap).any()
    assert len(ac) == len(panel) - 125 - 126
    assert len(_corr(rolling_correlations(panel, window=126), "A", "D")) == len(panel) - 125


def test_euler_contributions_sum_to_portfolio_volatility() -> None:
    cov = np.array([[0.04, 0.01, 0.0], [0.01, 0.09, -0.02], [0.0, -0.02, 0.01]])
    w = np.array([0.5, 0.3, 0.2])
    rc, sigma = euler_contributions(cov, w)
    assert sigma == pytest.approx(math.sqrt(w @ cov @ w))
    assert rc.sum() == pytest.approx(sigma)


def test_independent_equal_vol_assets_contribute_equally() -> None:
    rc, sigma = euler_contributions(np.eye(4) * 0.0004, np.full(4, 0.25))
    assert sigma == pytest.approx(0.01)  # sqrt(4 * 0.25^2 * 0.0004)
    assert rc == pytest.approx(np.full(4, 0.0025))


def test_rolling_euler_sums_to_rolling_portfolio_volatility() -> None:
    panel = _panel()
    weights = {"A": 0.4, "C": 0.35, "D": 0.25}
    result = rolling_euler_contributions(panel, weights, window=126)
    by_date = result.groupby("trade_date")
    port_vol = rolling_volatility(portfolio_returns(panel, weights), window=126).dropna()
    assert by_date["risk_contrib"].sum().to_numpy() == pytest.approx(port_vol.to_numpy())
    assert by_date["risk_contrib_pct"].sum().to_numpy() == pytest.approx(1.0)
    assert len(result) == 3 * (len(panel) - 125)


def test_rolling_euler_skips_incomplete_windows_and_rejects_unknown_holdings() -> None:
    panel = _panel()
    panel.iloc[10, 0] = np.nan
    result = rolling_euler_contributions(panel, {"A": 0.5, "C": 0.5}, window=126)
    assert result.trade_date.min() == panel.index[136]
    with pytest.raises(ValueError, match="Z"):
        rolling_euler_contributions(panel, {"A": 0.5, "Z": 0.5})


def test_single_holding_contributes_all_the_risk() -> None:
    panel = _panel()
    result = rolling_euler_contributions(panel, {"C": 1.0}, window=126)
    vol = rolling_volatility(panel["C"], window=126).dropna()
    assert result["risk_contrib"].to_numpy() == pytest.approx(vol.to_numpy())
    assert result["risk_contrib_pct"].to_numpy() == pytest.approx(1.0)


def test_constant_series_has_no_correlation_rows() -> None:
    panel = _panel()
    panel["K"] = 0.0123  # constant return: centred values are float noise, not dispersion
    result = rolling_correlations(panel, window=126)
    assert not ((result.ticker_a == "K") | (result.ticker_b == "K")).any()
    assert len(_corr(result, "A", "C")) == len(panel) - 125  # other pairs unaffected


def test_riskless_combination_has_no_euler_rows() -> None:
    panel = _panel()  # B = -2 A, so 2/3 A + 1/3 B is riskless up to float noise
    result = rolling_euler_contributions(panel, {"A": 2 / 3, "B": 1 / 3}, window=126)
    assert result.empty
    rc, sigma = euler_contributions(np.array([[1e-30]]), np.array([1.0]))
    assert sigma == 0.0 and np.isnan(rc).all()


def test_rolling_euler_ignores_the_future() -> None:
    panel = _panel()
    weights = {"A": 0.4, "C": 0.35, "D": 0.25}
    t = panel.index[200]
    changed = panel.copy()
    changed.iloc[201:] = 0.3
    before = rolling_euler_contributions(panel, weights, window=126)
    after = rolling_euler_contributions(changed, weights, window=126)
    assert before[before.trade_date <= t].equals(after[after.trade_date <= t])
