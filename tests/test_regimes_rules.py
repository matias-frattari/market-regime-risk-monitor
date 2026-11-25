# SYNTHETIC — not real market data. Index values and unemployment rates are made-up round
# numbers chosen to hit the rule boundaries; dates are arbitrary.
from dataclasses import replace
from datetime import date

import numpy as np
import pandas as pd
import pytest

from mrm.config import load_regime_rules
from mrm.regimes.rules import (
    COLUMNS,
    build_regimes,
    classify_level,
    history_label,
    monthly_sahm_gap,
    sahm_gap_on_days,
)

RULES = load_regime_rules()
SAHM = RULES.sahm
MONTHS = pd.date_range("2020-01-01", periods=17, freq="MS")
# m0..m13 flat at 4.0, then 4.3, 4.6, 4.6 -> 3m averages 4.1, 4.3, 4.5 -> gaps 0.1, 0.3, 0.5
UNRATE = [4.0] * 14 + [4.3, 4.6, 4.6]


def test_classify_level_boundaries() -> None:
    values = pd.Series([-0.01, 0.0, 0.99, 1.0, 2.5, np.nan])
    assert classify_level(values, 0.0, 1.0).tolist() == [
        "calm", "tension", "tension", "stress", "stress", None,
    ]  # fmt: skip


def test_history_label_uses_available_date() -> None:
    available = pd.Series(pd.to_datetime(["2011-06-01", "2011-06-02", None]))
    assert history_label(available, date(2011, 6, 2)).tolist() == ["backcast", "revised", None]


def test_sahm_gap_hand_computed() -> None:
    gap = monthly_sahm_gap(pd.Series(MONTHS), pd.Series(UNRATE), SAHM)
    assert gap.iloc[:12].isna().all()  # warm-up: needs 11 of the 12 prior 3m averages
    assert gap.iloc[12] == pytest.approx(0.0)
    assert gap.iloc[14:].tolist() == pytest.approx([0.1, 0.3, 0.5])


def test_sahm_gap_skips_a_month_never_published() -> None:
    keep = [i for i in range(17) if i != 15]  # like Oct-2025: no value at all
    gap = monthly_sahm_gap(pd.Series(MONTHS[keep]), pd.Series([UNRATE[i] for i in keep]), SAHM)
    assert gap.index[15] == MONTHS[15]
    assert gap.iloc[15] == pytest.approx((4.0 + 4.3) / 2 - 4.0)  # the month itself: 2 of 3
    assert gap.iloc[16] == pytest.approx((4.3 + 4.6) / 2 - 4.0)  # 2 of 3 months


def test_sahm_gap_strict_min_obs_leaves_gap_month_null() -> None:
    strict = replace(SAHM, ma_min_obs=3)
    keep = [i for i in range(17) if i != 15]
    gap = monthly_sahm_gap(pd.Series(MONTHS[keep]), pd.Series([UNRATE[i] for i in keep]), strict)
    assert np.isnan(gap.iloc[16])


def _daily(values: list[float], value_dates: list, start: str = "2021-05-03") -> pd.DataFrame:
    days = pd.bdate_range(start, periods=len(values))
    return pd.DataFrame(
        {
            "value": values,
            "value_date": pd.to_datetime(value_dates),
            "available_date": pd.to_datetime(value_dates),
            "age_days": [1] * len(values),
        },
        index=pd.DatetimeIndex(days, name="trade_date"),
    )


def test_sahm_gap_on_days_maps_current_month_and_flags_at_threshold() -> None:
    history = [(m, v) for m, v in zip(MONTHS, UNRATE, strict=True)]
    # each trading day carries one reference month (a forward-filled monthly series)
    frame = _daily([v for _, v in history], [m for m, _ in history])
    gap = sahm_gap_on_days(frame, SAHM)
    assert gap.iloc[-1] == pytest.approx(0.5)
    assert gap.iloc[0:12].isna().all()


def _inputs(nfci: list[float], stl: list[float], curve: list[float], unrate: list[float]):
    n = len(nfci)
    dates = ["2021-01-01"] * n
    return {
        "NFCI": _daily(nfci, dates),
        "STLFSI4": _daily(stl, dates),
        "T10Y2Y": _daily(curve, dates),
        "UNRATE": _daily(unrate, dates),
    }


def test_build_regimes_columns_and_flags() -> None:
    inputs = _inputs(
        nfci=[-0.5, 0.2, 1.4, np.nan],
        stl=[-1.0, 1.2, 1.0, 0.3],
        curve=[0.5, 0.0, -0.01, np.nan],
        unrate=[4.0, 4.0, 4.0, 4.0],
    )
    out = build_regimes(inputs, RULES)
    assert tuple(out.columns) == COLUMNS
    assert out["regime"].tolist() == ["calm", "tension", "stress", None]
    assert out["stlfsi4_regime"].tolist() == ["calm", "stress", "stress", "tension"]
    assert out["regime_agrees"].tolist() == [True, False, True, pd.NA]
    assert out["curve_inverted"].tolist() == [False, False, True, pd.NA]  # 0.0 is not inverted
    assert out["unemployment_rising"].isna().all()  # one month only: warm-up
    assert out["nfci_history"].tolist() == ["revised", "revised", "revised", "revised"]
    assert out["stlfsi4_history"].tolist() == ["backcast"] * 4  # available 2021 < 2022-11-17
