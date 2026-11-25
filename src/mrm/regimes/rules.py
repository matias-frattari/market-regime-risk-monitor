"""Rules-based regimes v1 (ADR 0006): pure functions, no database.

Inputs are point-in-time frames read from ``macro_asof`` (see ``mrm.regimes.io``): one per
series, indexed by trade_date, with columns ``value``, ``value_date``, ``available_date`` and
``age_days``. Every output row of a trading day depends only on the input rows of that day
(and, for the Sahm rule, on monthly values already published by then), so no rule looks ahead.
"""

from datetime import date

import numpy as np
import pandas as pd

from mrm.storage.models import LevelRule, RegimeRules, SahmRule

REGIME_LABELS: tuple[str, ...] = ("calm", "tension", "stress")
# Float tolerance of the Sahm comparison: UNRATE has one decimal, so 3-month averages are
# multiples of 1/30 and a gap of exactly 0.50 can come out as 0.4999999999999.
SAHM_TOLERANCE = 1e-9
COLUMNS: tuple[str, ...] = (
    "trade_date", "regime",
    "nfci_value", "nfci_value_date", "nfci_available_date", "nfci_age_days", "nfci_history",
    "stlfsi4_value", "stlfsi4_value_date", "stlfsi4_regime", "stlfsi4_history", "regime_agrees",
    "t10y2y_value", "t10y2y_value_date", "curve_inverted",
    "unrate_value", "unrate_value_date", "sahm_gap", "unemployment_rising",
)  # fmt: skip


def classify_level(values: pd.Series, tension_at: float, stress_at: float) -> pd.Series:
    """calm (< tension_at), tension ([tension_at, stress_at)), stress (>= stress_at).

    NaN stays None (no as-of value yet).
    """
    v = values.astype(float)
    labels = pd.Series([None] * len(values), index=values.index, dtype=object)
    calm, tension, stress = REGIME_LABELS
    labels[v < tension_at] = calm
    labels[(v >= tension_at) & (v < stress_at)] = tension
    labels[v >= stress_at] = stress
    return labels


def history_label(available_date: pd.Series, first_vintage: date) -> pd.Series:
    """``backcast`` when ``available_date < first_vintage`` (the index did not exist in real
    time when the value would have been usable), else ``revised``. None without a value."""
    available = pd.to_datetime(available_date)
    labels = np.where(available < pd.Timestamp(first_vintage), "backcast", "revised")
    return pd.Series(labels, index=available_date.index, dtype=object).where(
        available.notna(), None
    )


def _month(values: pd.Series | pd.Index) -> pd.DatetimeIndex:
    return pd.DatetimeIndex(pd.to_datetime(values)).to_period("M").to_timestamp()


def monthly_sahm_gap(value_dates: pd.Series, values: pd.Series, rule: SahmRule) -> pd.Series:
    """Sahm gap per reference month from the (value_date, value) pairs seen in ``macro_asof``.

    On a calendar-month grid (a month never published stays NaN): ``ma`` = mean of the last
    ``ma_months`` months (at least ``ma_min_obs``); gap = ``ma`` minus the minimum of ``ma``
    over the previous ``lookback_months`` months (at least ``lookback_min_obs``). The gap of
    month m uses months <= m only.
    """
    pairs = pd.DataFrame({"month": _month(value_dates), "value": values.astype(float)}).dropna()
    if pairs.empty:
        return pd.Series(dtype=float)
    monthly = pairs.groupby("month")["value"].last().sort_index()
    monthly = monthly.reindex(pd.date_range(monthly.index[0], monthly.index[-1], freq="MS"))
    ma = monthly.rolling(rule.ma_months, min_periods=rule.ma_min_obs).mean()
    prior_min = ma.shift(1).rolling(rule.lookback_months, min_periods=rule.lookback_min_obs).min()
    return ma - prior_min


def sahm_gap_on_days(frame: pd.DataFrame, rule: SahmRule) -> pd.Series:
    """Sahm gap of each trading day's current UNRATE reference month (NaN in warm-up)."""
    gaps = monthly_sahm_gap(frame["value_date"], frame["value"], rule)
    has_value = frame["value_date"].notna() & frame["value"].notna()
    months = pd.Series(pd.NaT, index=frame.index, dtype="datetime64[ns]")
    months[has_value] = _month(frame.loc[has_value, "value_date"])
    return pd.Series(gaps.reindex(months).to_numpy(), index=frame.index, dtype=float)


def _flag(condition: pd.Series, known: pd.Series) -> pd.Series:
    return condition.astype("boolean").where(known, pd.NA)


def _dates(series: pd.Series) -> pd.Series:
    return pd.to_datetime(series)


def _level(frame: pd.DataFrame, rule: LevelRule) -> tuple[pd.Series, pd.Series]:
    return (
        classify_level(frame["value"], rule.tension_at, rule.stress_at),
        history_label(frame["available_date"], rule.first_vintage),
    )


def build_regimes(inputs: dict[str, pd.DataFrame], rules: RegimeRules) -> pd.DataFrame:
    """One row per trading day of the primary signal's frame, columns ``COLUMNS``."""
    nfci = inputs[rules.regime.signal]
    days = nfci.index
    stl = inputs[rules.robustness.signal].reindex(days)
    curve = inputs[rules.curve_signal].reindex(days)
    unrate = inputs[rules.sahm.signal].reindex(days)

    regime, nfci_history = _level(nfci, rules.regime)
    stl_regime, stl_history = _level(stl, rules.robustness)
    gap = sahm_gap_on_days(unrate, rules.sahm)
    both = regime.notna() & stl_regime.notna()

    out = pd.DataFrame(
        {
            "trade_date": days,
            "regime": regime,
            "nfci_value": nfci["value"].astype(float),
            "nfci_value_date": _dates(nfci["value_date"]),
            "nfci_available_date": _dates(nfci["available_date"]),
            "nfci_age_days": nfci["age_days"].astype("Int64"),
            "nfci_history": nfci_history,
            "stlfsi4_value": stl["value"].astype(float),
            "stlfsi4_value_date": _dates(stl["value_date"]),
            "stlfsi4_regime": stl_regime,
            "stlfsi4_history": stl_history,
            "regime_agrees": _flag(regime == stl_regime, both),
            "t10y2y_value": curve["value"].astype(float),
            "t10y2y_value_date": _dates(curve["value_date"]),
            "curve_inverted": _flag(
                curve["value"].astype(float) < rules.curve_below, curve["value"].notna()
            ),
            "unrate_value": unrate["value"].astype(float),
            "unrate_value_date": _dates(unrate["value_date"]),
            "sahm_gap": gap,
            "unemployment_rising": _flag(
                gap >= rules.sahm.threshold_pp - SAHM_TOLERANCE, gap.notna()
            ),
        },
        index=days,
    )
    return out.reset_index(drop=True)[list(COLUMNS)]
