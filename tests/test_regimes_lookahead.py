# SYNTHETIC — not real market data (see tests/regimes_synthetic.py).
"""Non-regression: fact_regimes never uses information published after each trading day."""

from datetime import date, timedelta

import duckdb
import pandas as pd
import pytest

from mrm.config import load_macro_lags, load_regime_rules
from mrm.quality.calendar import nyse_business_days
from mrm.regimes import run
from mrm.regimes.io import read_regimes
from mrm.storage.db import connect
from mrm.storage.models import MacroObservation
from mrm.storage.schema import create_schema
from tests.regimes_synthetic import (
    DAYS,
    available_date,
    fridays,
    load_synthetic,
    synthetic_observations,
)

RULES = load_regime_rules()
NFCI_LAG = {lag.series_id: lag for lag in load_macro_lags()}["NFCI"]


def test_nfci_spike_used_only_after_its_release(con: duckdb.DuckDBPyConnection) -> None:
    days = nyse_business_days(date(2024, 1, 2), date(2024, 3, 29))
    spike = date(2024, 2, 9)
    assert spike not in {o.obs_date for o in NFCI_LAG.overrides}
    obs = [
        MacroObservation("NFCI", f, 2.0 if f == spike else -0.5)
        for f in fridays(date(2023, 12, 1), date(2024, 3, 29))
    ]
    load_synthetic(con, days=days, observations=obs)
    assert run.main([], con=con, rules=RULES, adjustments=[]) == 0
    regimes = read_regimes(con).set_index("trade_date")["regime"]
    regimes.index = regimes.index.date

    released = spike + timedelta(days=NFCI_LAG.lag_days)  # from config/macro_lags.yaml
    next_released = released + timedelta(days=7)
    stress_days = [d for d in days if released < d <= next_released]
    assert stress_days, "the spike must be visible on at least one trading day"
    assert all(regimes[d] == "stress" for d in stress_days)
    assert all(regimes[d] == "calm" for d in days if d not in stress_days)
    assert regimes[spike] == "calm"  # not on the reference date
    if released in regimes.index:
        assert regimes[released] == "calm"  # not on the release day itself


def _perturbed(o: MacroObservation) -> MacroObservation:
    return MacroObservation(o.series_id, o.obs_date, None if o.value is None else 9 - 3 * o.value)


def _run_on(days: list[date], observations: list[MacroObservation]) -> pd.DataFrame:
    con = connect(":memory:")
    try:
        create_schema(con)
        load_synthetic(con, days=days, observations=observations)
        assert run.main([], con=con, rules=RULES, adjustments=[]) == 0
        return read_regimes(con)
    finally:
        con.close()


@pytest.mark.parametrize("cutoff", [date(2022, 9, 14), date(2023, 6, 1), date(2024, 1, 10)])
@pytest.mark.parametrize("mode", ["drop", "perturb"])
def test_rows_up_to_cutoff_ignore_later_information(cutoff: date, mode: str) -> None:
    """Rows with trade_date <= D must not change when everything published on or after D is
    removed or altered (all four series, incl. the Sahm months and the never-published one)."""
    full_obs = synthetic_observations()
    full = _run_on(DAYS, full_obs)

    def future(o: MacroObservation) -> bool:
        return available_date(o.series_id, o.obs_date) >= cutoff

    if mode == "drop":
        obs = [o for o in full_obs if not future(o)]
        days = [d for d in DAYS if d <= cutoff]
    else:
        obs = [_perturbed(o) if future(o) else o for o in full_obs]
        days = DAYS
    partial = _run_on(days, obs)

    def upto(frame: pd.DataFrame) -> pd.DataFrame:
        return frame[frame["trade_date"].dt.date <= cutoff].reset_index(drop=True)

    expected, got = upto(full), upto(partial)
    assert len(expected) == len([d for d in DAYS if d <= cutoff])
    pd.testing.assert_frame_equal(got, expected)
