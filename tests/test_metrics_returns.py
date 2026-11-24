"""Returns and risk-free conversion on SYNTHETIC series with known results."""

import math

import numpy as np
import pandas as pd
import pytest

from mrm.metrics.returns import (
    daily_risk_free,
    excess_returns,
    log_returns,
    portfolio_returns,
    simple_returns,
)

DATES = pd.date_range("2024-01-01", periods=5, freq="B")


def test_simple_and_log_returns_known_values() -> None:
    prices = pd.DataFrame({"A": [100.0, 110.0, 99.0, 99.0, 108.9]}, index=DATES)
    simple = simple_returns(prices)["A"]
    assert math.isnan(simple.iloc[0])
    assert simple.iloc[1:].tolist() == pytest.approx([0.10, -0.10, 0.0, 0.10])
    log = log_returns(prices)["A"]
    assert log.iloc[1:].tolist() == pytest.approx(np.log1p(simple.iloc[1:]).tolist())


def test_missing_quote_never_spans_a_gap() -> None:
    prices = pd.DataFrame({"A": [100.0, np.nan, 120.0, 132.0, 132.0]}, index=DATES)
    simple = simple_returns(prices)["A"]
    assert simple.iloc[:3].isna().all()  # day 1 missing -> days 1 and 2 have no return
    assert simple.iloc[3] == pytest.approx(0.10)


def test_non_positive_or_unsorted_prices_are_rejected() -> None:
    with pytest.raises(ValueError, match="> 0"):
        simple_returns(pd.DataFrame({"A": [1.0, 0.0, 1.0, 1.0, 1.0]}, index=DATES))
    with pytest.raises(ValueError, match="ascending"):
        simple_returns(pd.DataFrame({"A": [1.0] * 5}, index=DATES[::-1]))


def test_portfolio_return_is_weighted_sum_and_nan_if_any_holding_missing() -> None:
    returns = pd.DataFrame(
        {"A": [0.01, 0.02, np.nan], "B": [0.03, -0.01, 0.01], "C": [9.0, 9.0, 9.0]},
        index=DATES[:3],
    )
    result = portfolio_returns(returns, {"A": 0.6, "B": 0.4})
    assert result.iloc[0] == pytest.approx(0.6 * 0.01 + 0.4 * 0.03)
    assert result.iloc[1] == pytest.approx(0.6 * 0.02 - 0.4 * 0.01)
    assert math.isnan(result.iloc[2])


def test_portfolio_with_unknown_holding_is_rejected() -> None:
    returns = pd.DataFrame({"A": [0.01]}, index=DATES[:1])
    with pytest.raises(ValueError, match="Z"):
        portfolio_returns(returns, {"A": 0.5, "Z": 0.5})


def test_daily_risk_free_compounds_back_to_annual_and_keeps_nan() -> None:
    rf = daily_risk_free(pd.Series([5.0, 0.0, np.nan]))
    assert rf.iloc[0] == pytest.approx(1.05 ** (1 / 252) - 1)
    assert (1 + rf.iloc[0]) ** 252 == pytest.approx(1.05)
    assert rf.iloc[1] == 0.0
    assert math.isnan(rf.iloc[2])


def test_excess_returns_nan_where_risk_free_missing() -> None:
    r = pd.Series([0.01, 0.02, 0.03], index=DATES[:3])
    rf = pd.Series([np.nan, 0.001], index=DATES[:2])  # NULL at start, absent at the end
    result = excess_returns(r, rf)
    assert math.isnan(result.iloc[0])
    assert result.iloc[1] == pytest.approx(0.019)
    assert math.isnan(result.iloc[2])
