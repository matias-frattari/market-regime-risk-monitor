"""SYNTHETIC — not real market data. Helpers that load made-up NFCI, STLFSI4, T10Y2Y and
UNRATE observations (seeded random walks and round numbers on real calendar dates) and a flat
SPY calendar, only to exercise ``mrm regimes`` end to end."""

from datetime import date, timedelta

import duckdb
import numpy as np

from mrm.config import load_macro_lags
from mrm.quality.calendar import nyse_business_days
from mrm.storage.loaders import upsert_macro, upsert_prices
from mrm.storage.models import MacroObservation, PriceBar

LAGS = {lag.series_id: lag for lag in load_macro_lags()}
DAYS = nyse_business_days(date(2022, 1, 3), date(2024, 6, 28))
GAP_MONTH = date(2023, 10, 1)  # like Oct-2025: never published ('.' on FRED)


def available_date(series_id: str, obs_date: date) -> date:
    """Release date by config/macro_lags.yaml (override if listed, else obs_date + lag)."""
    lag = LAGS[series_id]
    overrides = {o.obs_date: o.available_date for o in lag.overrides}
    return overrides.get(obs_date, obs_date + timedelta(days=lag.lag_days))


def fridays(start: date, end: date) -> list[date]:
    first = start + timedelta(days=(4 - start.weekday()) % 7)
    return [first + timedelta(weeks=i) for i in range((end - first).days // 7 + 1)]


def month_starts(start: date, end: date) -> list[date]:
    months, day = [], date(start.year, start.month, 1)
    while day <= end:
        months.append(day)
        day = date(day.year + day.month // 12, day.month % 12 + 1, 1)
    return months


def load_calendar(con: duckdb.DuckDBPyConnection, days: list[date]) -> None:
    upsert_prices(con, [PriceBar("SPY", d, 100.0, 100.0, 100.0, 100.0, 1000) for d in days])


def synthetic_observations(seed: int = 7) -> list[MacroObservation]:
    rng = np.random.default_rng(seed)
    weeks = fridays(date(2021, 11, 5), date(2024, 6, 21))
    nfci = np.cumsum(rng.normal(0.0, 0.25, len(weeks))) - 0.3
    stl = nfci + rng.normal(0.0, 0.4, len(weeks))
    obs = [MacroObservation("NFCI", d, float(v)) for d, v in zip(weeks, nfci, strict=True)]
    obs += [MacroObservation("STLFSI4", d, float(v)) for d, v in zip(weeks, stl, strict=True)]
    curve_days = nyse_business_days(date(2021, 12, 1), date(2024, 6, 28))
    obs += [
        MacroObservation("T10Y2Y", d, round(0.8 * np.sin(i / 90.0), 2))
        for i, d in enumerate(curve_days)
    ]
    months = month_starts(date(2020, 6, 1), date(2024, 5, 1))
    rates = 3.5 + np.round(np.maximum(0, np.arange(len(months)) - 30) * 0.1, 1)
    obs += [
        MacroObservation("UNRATE", m, None if m == GAP_MONTH else float(r))
        for m, r in zip(months, rates, strict=True)
    ]
    return obs


def load_synthetic(
    con: duckdb.DuckDBPyConnection,
    days: list[date] = DAYS,
    observations: list[MacroObservation] | None = None,
) -> None:
    load_calendar(con, days)
    upsert_macro(con, synthetic_observations() if observations is None else observations)
