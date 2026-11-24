"""Risk metrics on SYNTHETIC series with analytically known results."""

import math

import numpy as np
import pandas as pd
import pytest

from mrm.metrics.returns import simple_returns
from mrm.metrics.risk import (
    annualized_return,
    annualized_return_from_prices,
    annualized_volatility,
    drawdown,
    historical_var_cvar,
    max_drawdown,
    parametric_var_cvar,
    price_drawdown,
    rolling_volatility,
    sharpe_ratio,
)


def _series(values: list[float] | np.ndarray) -> pd.Series:
    return pd.Series(values, index=pd.date_range("2024-01-01", periods=len(values), freq="B"))


def test_constant_return_has_zero_volatility() -> None:
    r = _series([0.001] * 100)
    assert annualized_volatility(r) == pytest.approx(0.0, abs=1e-15)
    assert rolling_volatility(r, window=60).dropna().abs().max() == pytest.approx(0.0, abs=1e-12)


def test_alternating_returns_have_known_volatility() -> None:
    a, n = 0.01, 100  # +a, -a alternating: mean 0, sample var = n a^2 / (n - 1)
    r = _series([a if i % 2 == 0 else -a for i in range(n)])
    assert annualized_volatility(r) == pytest.approx(a * math.sqrt(n / (n - 1)) * math.sqrt(252))


def test_rolling_volatility_needs_a_full_window_and_ignores_the_future() -> None:
    rng = np.random.default_rng(0)  # synthetic noise, not market data
    r = _series(rng.normal(0, 0.01, 200))
    vol = rolling_volatility(r, window=60)
    assert vol.iloc[:59].isna().all() and vol.iloc[59:].notna().all()
    assert vol.iloc[99] == pytest.approx(r.iloc[40:100].std(ddof=1) * math.sqrt(252))
    future_changed = r.copy()
    future_changed.iloc[100:] = 0.5
    assert rolling_volatility(future_changed, window=60).iloc[99] == vol.iloc[99]
    with_gap = r.copy()
    with_gap.iloc[120] = np.nan
    gap_vol = rolling_volatility(with_gap, window=60)
    assert gap_vol.iloc[120:180].isna().all() and gap_vol.iloc[180:].notna().all()


def test_drawdown_of_known_path() -> None:
    # prices 100 -> 120 -> 90 -> 110: peak 120, trough 90 -> MDD -25%
    r = _series([0.20, -0.25, 110 / 90 - 1])
    assert drawdown(r).tolist() == pytest.approx([0.0, -0.25, 110 / 120 - 1])
    assert max_drawdown(r) == pytest.approx(-0.25)


def test_drawdown_counts_a_first_day_loss_and_keeps_nan_days() -> None:
    r = _series([-0.10, np.nan, 0.05])
    dd = drawdown(r)
    assert dd.iloc[0] == pytest.approx(-0.10)
    assert math.isnan(dd.iloc[1])
    assert dd.iloc[2] == pytest.approx(0.9 * 1.05 - 1)


def test_annualized_return_of_constant_daily_return() -> None:
    assert annualized_return(_series([0.0004] * 252)) == pytest.approx(1.0004**252 - 1)


def test_price_drawdown_keeps_a_move_across_a_missing_quote() -> None:
    # Return-based drawdown loses the -20% move: the return spanning the gap is NaN.
    close = _series([100.0, 100.0, np.nan, 80.0, 80.0])
    assert max_drawdown(simple_returns(close.to_frame())[0]) == 0.0
    dd = price_drawdown(close)
    assert math.isnan(dd.iloc[0]) and math.isnan(dd.iloc[2])  # first close, no close
    assert dd.iloc[[1, 3, 4]].tolist() == pytest.approx([0.0, -0.20, -0.20])


def test_annualized_return_from_prices_counts_intervals_across_a_gap() -> None:
    close = _series([np.nan, 100.0, np.nan, 121.0, np.nan])  # 2 intervals, +21%
    assert annualized_return_from_prices(close) == pytest.approx(1.21 ** (252 / 2) - 1)
    assert math.isnan(annualized_return_from_prices(_series([np.nan, 100.0, np.nan])))


def test_level_metrics_equal_return_based_metrics_without_gaps() -> None:
    rng = np.random.default_rng(7)  # synthetic random walk, not market data
    close = _series(100.0 * np.exp(np.cumsum(rng.normal(0.0003, 0.012, 5000))))
    r = simple_returns(close.to_frame())[0]  # 4999 returns
    old = annualized_return(r)
    assert old == pytest.approx(math.exp(np.log1p(r.dropna()).mean() * 252) - 1, rel=1e-15)
    assert annualized_return_from_prices(close) == pytest.approx(old, rel=1e-12, abs=0)
    new_dd, old_dd = price_drawdown(close), drawdown(r)
    assert new_dd.isna().equals(old_dd.isna())
    assert np.allclose(new_dd.dropna(), old_dd.dropna(), rtol=0, atol=1e-12)


def test_sharpe_known_value_and_degenerate_cases() -> None:
    a = 0.01
    x = _series([0.002 + a, 0.002 - a] * 50)  # mean 0.002, std a*sqrt(100/99)
    assert sharpe_ratio(x) == pytest.approx(0.002 / (a * math.sqrt(100 / 99)) * math.sqrt(252))
    assert math.isnan(sharpe_ratio(_series([0.001] * 10)))  # zero dispersion
    assert math.isnan(sharpe_ratio(_series([np.nan, 0.01])))  # NaN dropped -> 1 obs


def test_historical_var_cvar_of_known_discrete_distribution() -> None:
    # returns (k - 50) / 1000 for k = 1..100; 5% quantile at position 4.95 -> k = 5.95
    r = _series([(k - 50) / 1000 for k in range(1, 101)])
    var, cvar = historical_var_cvar(r)
    assert var == pytest.approx(0.04405)
    assert cvar == pytest.approx(0.047)  # mean of k = 1..5 -> (3 - 50) / 1000
    assert cvar >= var


def test_parametric_var_cvar_matches_closed_form() -> None:
    mu, sigma = 0.0005, 0.012
    z = np.random.default_rng(1).normal(size=1000)  # synthetic
    r = _series(mu + sigma * (z - z.mean()) / z.std(ddof=1))  # exact sample mean/std
    var, cvar = parametric_var_cvar(r)
    assert var == pytest.approx(-(mu - 1.6448536269514722 * sigma))
    assert cvar == pytest.approx(-(mu - 2.0627128075074257 * sigma))


def test_var_with_no_data_is_nan() -> None:
    empty = _series([np.nan, np.nan])
    assert all(math.isnan(v) for v in historical_var_cvar(empty) + parametric_var_cvar(empty))


def test_drawdown_ignores_the_future() -> None:
    rng = np.random.default_rng(3)  # synthetic random walk, not market data
    close = _series(100.0 * np.exp(np.cumsum(rng.normal(0, 0.01, 200))))
    changed = close.copy()
    changed.iloc[101:] = changed.iloc[101:] * 3.0  # a new peak after t = 100
    t = slice(0, 101)
    assert price_drawdown(changed).iloc[t].equals(price_drawdown(close).iloc[t])
    r, r_changed = simple_returns(close.to_frame())[0], simple_returns(changed.to_frame())[0]
    assert drawdown(r_changed).iloc[t].equals(drawdown(r).iloc[t])
