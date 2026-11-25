# SYNTHETIC — not real market data (see tests/regimes_synthetic.py).
from dataclasses import replace
from datetime import date

import duckdb
import pytest

from mrm.config import load_regime_rules
from mrm.regimes import run
from mrm.regimes.io import read_regimes
from mrm.regimes.report import nulls_after_warmup, stale_runs
from mrm.storage.models import KnownEvent
from tests.regimes_synthetic import DAYS, fridays, load_synthetic, synthetic_observations

RULES = load_regime_rules()


def _count(con: duckdb.DuckDBPyConnection, sql: str) -> int:
    return con.execute(sql).fetchone()[0]


@pytest.fixture
def loaded(con: duckdb.DuckDBPyConnection) -> duckdb.DuckDBPyConnection:
    load_synthetic(con)
    return con


def test_one_row_per_trading_day(loaded: duckdb.DuckDBPyConnection) -> None:
    assert run.main([], con=loaded, rules=RULES, adjustments=[]) == 0
    assert _count(loaded, "SELECT count(*) FROM fact_regimes") == len(DAYS)
    assert _count(loaded, "SELECT count(DISTINCT trade_date) FROM fact_regimes") == len(DAYS)
    assert _count(loaded, "SELECT count(*) FROM fact_regimes WHERE regime IS NULL") == 0


def test_nulls_only_in_sahm_warmup(loaded: duckdb.DuckDBPyConnection) -> None:
    run.main([], con=loaded, rules=RULES, adjustments=[])
    regimes = read_regimes(loaded)
    null_columns = {c for c in regimes.columns if regimes[c].isna().any()}
    assert null_columns == {"sahm_gap", "unemployment_rising"}
    # the never-published month does not create NULLs after the warm-up
    assert nulls_after_warmup(regimes, "sahm_gap") == 0
    assert regimes["unemployment_rising"].astype("boolean").fillna(False).any()


def test_dry_run_writes_nothing(
    loaded: duckdb.DuckDBPyConnection, capsys: pytest.CaptureFixture[str]
) -> None:
    assert run.main(["--dry-run"], con=loaded, rules=RULES, adjustments=[]) == 0
    assert _count(loaded, "SELECT count(*) FROM fact_regimes") == 0
    assert "dry run: nothing written" in capsys.readouterr().out


def test_rerun_is_idempotent(loaded: duckdb.DuckDBPyConnection) -> None:
    run.main([], con=loaded, rules=RULES, adjustments=[])
    first = read_regimes(loaded)
    run.main([], con=loaded, rules=RULES, adjustments=[])
    assert read_regimes(loaded).equals(first)


def test_report_sections(
    loaded: duckdb.DuckDBPyConnection, capsys: pytest.CaptureFixture[str]
) -> None:
    run.main([], con=loaded, rules=RULES, adjustments=[])
    out = capsys.readouterr().out
    for text in (
        f"fact_regimes: {len(DAYS)} trading days",
        "regime (NFCI)",
        "curve_inverted",
        "unemployment_rising",
        "regime changes per year",
        "NFCI vs STLFSI4 agreement",
        "NULL values",
        "stale NFCI (nfci_age_days > 14)",
        "written (full rebuild, one transaction)",
    ):
        assert text in out


def test_stale_runs_labelled_with_overlapping_event(con: duckdb.DuckDBPyConnection) -> None:
    missing = set(fridays(date(2023, 3, 3), date(2023, 3, 24)))  # 4 weekly releases skipped
    obs = [
        o for o in synthetic_observations() if not (o.series_id == "NFCI" and o.obs_date in missing)
    ]
    load_synthetic(con, observations=obs)
    event = KnownEvent("synthetic outage", date(2023, 3, 20), date(2023, 3, 31), "test")
    rules = replace(RULES, known_events=(event,))
    run.main([], con=con, rules=rules, adjustments=[])
    runs = stale_runs(read_regimes(con), rules.nfci_stale_days, rules.known_events)
    # the NFCI overrides of config/macro_lags.yaml (2022-12-16/23 released 2023-01-05) also
    # leave a stale run on these synthetic Fridays; it overlaps no known event
    assert [(r.start, r.events) for r in runs] == [
        (date(2022, 12, 27), ()),
        (date(2023, 3, 13), ("synthetic outage",)),  # 2023-02-24 + 14 d = 03-10 (Fri)
    ]
    assert all(r.max_age_days > 14 for r in runs)
    assert runs[1].value_dates == (date(2023, 2, 24),)


def test_off_calendar_day_is_discarded(
    loaded: duckdb.DuckDBPyConnection, capsys: pytest.CaptureFixture[str]
) -> None:
    from tests.regimes_synthetic import load_calendar

    load_calendar(loaded, [date(2023, 7, 1)])  # a Saturday
    run.main([], con=loaded, rules=RULES, adjustments=[])
    assert _count(loaded, "SELECT count(*) FROM fact_regimes") == len(DAYS)
    assert "off-calendar days discarded: 1" in capsys.readouterr().out


def test_empty_warehouse_exits_1(con: duckdb.DuckDBPyConnection) -> None:
    assert run.main([], con=con, rules=RULES, adjustments=[]) == 1


def test_invalid_config_exits_2(
    monkeypatch: pytest.MonkeyPatch, con: duckdb.DuckDBPyConnection
) -> None:
    def broken() -> None:
        raise ValueError("regime.stress_at must be greater than regime.tension_at")

    monkeypatch.setattr(run, "load_regime_rules", broken)
    assert run.main([], con=con, adjustments=[]) == 2


def test_nfci_null_before_series_starts(con: duckdb.DuckDBPyConnection) -> None:
    late = [
        o
        for o in synthetic_observations()
        if not (o.series_id == "NFCI" and o.obs_date < date(2022, 3, 1))
    ]
    load_synthetic(con, observations=late)
    run.main([], con=con, rules=RULES, adjustments=[])
    regimes = read_regimes(con)
    assert regimes["regime"].isna().sum() > 0
    assert regimes.loc[regimes["regime"].isna(), "nfci_value"].isna().all()
