# SYNTHETIC — not real market data. Prices are flat made-up numbers (100.0) and macro values
# are round placeholders, used only to force each data quality check to fire or stay quiet.
from datetime import date, timedelta

import duckdb
import pytest

from mrm.config import DataQualityConfig, ReturnException
from mrm.quality import run
from mrm.quality.calendar import nyse_business_days
from mrm.quality.checks import (
    check_future_dates,
    check_macro_freshness,
    check_macro_gaps,
    check_price_duplicates,
    check_price_extreme_returns,
    check_price_missing_dates,
    check_price_non_positive,
    check_price_staleness,
    run_checks,
)
from mrm.quality.report import exit_code
from mrm.storage.loaders import insert_dq_results, upsert_assets, upsert_macro, upsert_prices
from mrm.storage.models import Asset, Finding, MacroObservation, PriceBar

TODAY = date(2026, 11, 18)  # Wednesday
START = date(2026, 10, 1)
CARTER = date(2025, 1, 9)

CFG = DataQualityConfig(
    special_closures={CARTER: "SYNTHETIC closure"},
    max_missing_gap_days=3,
    extreme_return_abs=0.15,
    extreme_return_top_n=3,
    stale_after_business_days=3,
    known_extreme_returns=(
        ReturnException(date(2020, 3, 1), date(2020, 4, 30), "SYNTHETIC crash"),
    ),
    series_frequency={"DGS10": "daily", "NFCI": "weekly", "UNRATE": "monthly"},
    max_age_days={"daily": 7, "weekly": 16, "monthly": 80},
    classify_gaps_from=date(1986, 1, 1),
    known_gaps={("UNRATE", date(2025, 10, 1)): "SYNTHETIC shutdown"},
)


def bars(ticker: str, days: list[date], closes: list[float] | None = None) -> list[PriceBar]:
    """SYNTHETIC — flat 100.0 bars unless ``closes`` is given."""
    closes = closes or [100.0] * len(days)
    return [PriceBar(ticker, d, c, c, c, c, 1000) for d, c in zip(days, closes)]


def trading_days(start: date = START, end: date = TODAY) -> list[date]:
    return nyse_business_days(start, end, CFG.special_closures)


def load_clean(con: duckdb.DuckDBPyConnection) -> None:
    """SYNTHETIC — a warehouse that passes every check."""
    upsert_assets(con, [Asset("SPY", "equity_us"), Asset("TLT", "bonds")])
    upsert_prices(con, bars("SPY", trading_days()) + bars("TLT", trading_days()))
    upsert_macro(
        con,
        [
            MacroObservation("DGS10", date(2026, 11, 17), 4.0),
            MacroObservation("NFCI", date(2026, 11, 6), -0.5),
            MacroObservation("UNRATE", date(2026, 10, 1), 4.0),
        ],
    )


def by_key(findings: list[Finding]) -> dict[tuple[str, str, str], Finding]:
    return {(f.check, f.entity, f.severity): f for f in findings}


# --- clean -----------------------------------------------------------------------------------


def test_clean_warehouse_has_no_findings(con: duckdb.DuckDBPyConnection) -> None:
    load_clean(con)
    findings = run_checks(con, CFG, TODAY)
    assert findings == []
    assert exit_code(findings) == 0


# --- prices ----------------------------------------------------------------------------------


def test_duplicates_are_critical(con: duckdb.DuckDBPyConnection) -> None:
    # raw_prices has a PK, so duplicates are forced in a PK-less copy of its shape.
    con.execute("CREATE TABLE prices_no_pk (ticker VARCHAR, trade_date DATE, close DOUBLE)")
    con.execute(
        "INSERT INTO prices_no_pk VALUES "
        "('SPY', '2026-11-16', 100), ('SPY', '2026-11-16', 101), ('SPY', '2026-11-17', 100)"
    )
    findings = check_price_duplicates(con, CFG, TODAY, table="prices_no_pk")
    assert findings == [Finding("price_duplicates", "SPY", "critical", 1, "2026-11-16 (x2)")]
    assert check_price_duplicates(con, CFG, TODAY) == []


def test_non_positive_prices_are_critical(con: duckdb.DuckDBPyConnection) -> None:
    days = trading_days()
    rows = bars("SPY", days)
    rows[3] = PriceBar("SPY", days[3], 100.0, 100.0, 0.0, 100.0, 1000)  # low = 0
    rows[5] = PriceBar("SPY", days[5], 100.0, 100.0, 100.0, -1.0, 1000)  # close < 0
    upsert_prices(con, rows)
    (finding,) = check_price_non_positive(con, CFG, TODAY)
    assert (finding.check, finding.severity, finding.n_affected) == (
        "price_non_positive",
        "critical",
        2,
    )
    assert str(days[3]) in finding.detail and str(days[5]) in finding.detail


def test_missing_dates_short_gap_warning_long_gap_critical(
    con: duckdb.DuckDBPyConnection,
) -> None:
    days = trading_days()
    short = {days[5]}
    long = set(days[10:15])  # 5 consecutive trading days > max_missing_gap_days=3
    upsert_prices(con, bars("SPY", [d for d in days if d not in short | long]))
    found = by_key(check_price_missing_dates(con, CFG, TODAY))
    assert found[("price_missing_dates", "SPY", "warning")].n_affected == 1
    critical = found[("price_missing_dates", "SPY", "critical")]
    assert critical.n_affected == 5
    assert f"{days[10]}..{days[14]} (5d)" in critical.detail


def test_missing_dates_start_at_each_tickers_first_date(con: duckdb.DuckDBPyConnection) -> None:
    days = trading_days()
    upsert_prices(con, bars("SPY", days) + bars("NEW", days[20:]))  # NEW listed later
    assert check_price_missing_dates(con, CFG, TODAY) == []


def test_bars_on_non_trading_days_are_warnings(con: duckdb.DuckDBPyConnection) -> None:
    saturday = date(2026, 11, 14)
    upsert_prices(con, bars("SPY", sorted([*trading_days(), saturday])))
    (finding,) = check_price_missing_dates(con, CFG, TODAY)
    assert finding == Finding("price_non_trading_dates", "SPY", "warning", 1, str(saturday))


def test_special_closure_is_not_a_missing_date(con: duckdb.DuckDBPyConnection) -> None:
    days = nyse_business_days(date(2025, 1, 6), date(2025, 1, 13))
    assert CARTER in days  # a trading day by rule ...
    upsert_prices(con, bars("SPY", [d for d in days if d != CARTER]))
    assert check_price_missing_dates(con, CFG, TODAY) == []  # ... but closed by config


def test_extreme_returns_warning_info_and_top_n(con: duckdb.DuckDBPyConnection) -> None:
    days = nyse_business_days(date(2020, 1, 2), date(2020, 6, 30))
    closes = [100.0] * len(days)
    jumps = {5: 1.30, 10: 0.80, 15: 1.20, 25: 1.18, 26: 0.75}  # Jan-Feb: flagged
    level = 100.0
    for i in range(len(days)):
        level *= jumps.get(i, 1.0)
        closes[i] = level
    in_window = next(i for i, d in enumerate(days) if d == date(2020, 3, 16))
    closes[in_window:] = [c * 0.7 for c in closes[in_window:]]  # -30% inside the window
    upsert_prices(con, bars("XLE", days, closes))

    found = by_key(check_price_extreme_returns(con, CFG, TODAY))
    warning = found[("price_extreme_returns", "XLE", "warning")]
    assert warning.n_affected == 5
    assert warning.detail.count("%") == 1 + 3  # threshold + top 3 only
    assert f"{days[5]} +30.00%" in warning.detail
    assert f"{days[26]} -25.00%" in warning.detail
    info = found[("price_extreme_returns", "XLE", "info")]
    assert info.n_affected == 1 and "SYNTHETIC crash" in info.detail


def test_staleness_uses_last_trading_day(con: duckdb.DuckDBPyConnection) -> None:
    upsert_assets(con, [Asset("SPY", "equity_us"), Asset("TLT", "bonds"), Asset("GLD", "gold")])
    friday, monday = date(2026, 11, 20), date(2026, 11, 23)
    upsert_prices(con, bars("SPY", trading_days(START, friday)))
    upsert_prices(con, bars("TLT", trading_days(START, date(2026, 11, 16))))
    # Saturday: the last trading day is Friday, so SPY is fresh (TLT is 4 days behind).
    saturday = by_key(check_price_staleness(con, CFG, date(2026, 11, 21)))
    assert {k[1] for k in saturday} == {"TLT", "GLD"}
    assert saturday[("price_staleness", "TLT", "critical")].n_affected == 4
    # Monday: SPY 1 day behind (ok); TLT 17,18,19,20,23 = 5 > 3 (critical); GLD has no data.
    found = by_key(check_price_staleness(con, CFG, monday))
    assert set(found) == {
        ("price_staleness", "TLT", "critical"),
        ("price_staleness", "GLD", "critical"),
    }
    assert found[("price_staleness", "TLT", "critical")].n_affected == 5
    assert found[("price_staleness", "GLD", "critical")].detail == "no price data"


# --- macro -----------------------------------------------------------------------------------


def test_macro_freshness(con: duckdb.DuckDBPyConnection) -> None:
    upsert_macro(
        con,
        [
            MacroObservation("DGS10", date(2026, 11, 1), 4.0),  # 17 days > 7
            MacroObservation("DGS10", date(2026, 11, 17), None),  # missing: not fresh data
            MacroObservation("NFCI", date(2026, 11, 6), -0.5),  # 12 days <= 16
            MacroObservation("OTHER", date(2026, 11, 17), 1.0),  # no frequency configured
        ],
    )
    found = by_key(check_macro_freshness(con, CFG, TODAY))
    assert set(found) == {
        ("macro_freshness", "DGS10", "critical"),
        ("macro_freshness", "UNRATE", "critical"),
        ("macro_freshness", "OTHER", "warning"),
    }
    assert found[("macro_freshness", "DGS10", "critical")].n_affected == 17
    assert found[("macro_freshness", "UNRATE", "critical")].detail == "no data"


def test_macro_gaps_classification(con: duckdb.DuckDBPyConnection) -> None:
    nulls = {
        ("DGS10", date(2024, 3, 29)),  # Good Friday -> NYSE holiday
        ("DGS10", date(2024, 11, 11)),  # Veterans Day -> federal holiday
        ("DGS10", CARTER),  # special closure from config
        ("DGS10", date(1980, 2, 12)),  # before classify_gaps_from
        ("DGS10", date(2024, 11, 12)),  # ordinary Tuesday -> unexplained
        ("UNRATE", date(2025, 10, 1)),  # known gap
        ("UNRATE", date(2025, 1, 1)),  # monthly: Jan 1 is NOT treated as a holiday
    }
    upsert_macro(con, [MacroObservation(s, d, None) for s, d in nulls])
    upsert_macro(con, [MacroObservation("DGS10", date(2024, 11, 13), 4.0)])  # not a gap
    found = by_key(check_macro_gaps(con, CFG, TODAY))

    assert found[("macro_gaps", "DGS10", "warning")] == Finding(
        "macro_gaps", "DGS10", "warning", 1, "unexplained: 2024-11-12"
    )
    info = found[("macro_gaps", "DGS10", "info")]
    assert info.n_affected == 4
    for label in ("NYSE holiday 1", "federal holiday 1", "special closure 1", "(not classified) 1"):
        assert label in info.detail
    assert found[("macro_gaps", "UNRATE", "warning")].detail == "unexplained: 2025-01-01"
    assert "2025-10-01 (SYNTHETIC shutdown)" in found[("macro_gaps", "UNRATE", "info")].detail

    # info + warning for the same series must not collide in dq_results.
    assert insert_dq_results(con, "run-1", list(found.values())) == 4


def test_future_dates_are_critical(con: duckdb.DuckDBPyConnection) -> None:
    load_clean(con)
    tomorrow = TODAY + timedelta(days=1)
    upsert_prices(con, bars("SPY", [tomorrow]))
    upsert_macro(con, [MacroObservation("NFCI", date(2026, 12, 4), 0.0)])
    found = by_key(check_future_dates(con, CFG, TODAY))
    assert set(found) == {
        ("future_dates", "raw_prices:SPY", "critical"),
        ("future_dates", "raw_macro:NFCI", "critical"),
    }


# --- run / report / persistence -----------------------------------------------------------


def dq_rows(con: duckdb.DuckDBPyConnection) -> list[tuple]:
    return con.execute(
        "SELECT run_id, check_name, entity, severity, n_affected FROM dq_results ORDER BY 2, 3"
    ).fetchall()


def test_main_clean_run_exit_0_and_summary_row(
    con: duckdb.DuckDBPyConnection, capsys: pytest.CaptureFixture[str]
) -> None:
    load_clean(con)
    assert run.main([], con=con, cfg=CFG, today=TODAY) == 0
    rows = dq_rows(con)
    assert [(r[1], r[2], r[3], r[4]) for r in rows] == [("run", "all", "info", 0)]
    assert "PASS: critical=0 warning=0 info=0" in capsys.readouterr().out


def test_main_warnings_only_exit_0(con: duckdb.DuckDBPyConnection) -> None:
    load_clean(con)
    upsert_macro(con, [MacroObservation("DGS10", date(2024, 11, 12), None)])  # unexplained
    assert run.main([], con=con, cfg=CFG, today=TODAY) == 0
    assert ("macro_gaps", "DGS10", "warning", 1) in [r[1:] for r in dq_rows(con)]


def test_main_critical_exit_1_and_persisted(
    con: duckdb.DuckDBPyConnection, capsys: pytest.CaptureFixture[str]
) -> None:
    load_clean(con)
    upsert_prices(con, bars("SPY", [TODAY + timedelta(days=1)]))
    assert run.main([], con=con, cfg=CFG, today=TODAY) == 1
    rows = [r[1:] for r in dq_rows(con)]
    assert ("future_dates", "raw_prices:SPY", "critical", 1) in rows
    assert ("run", "all", "info", 1) in rows
    out = capsys.readouterr().out
    assert "CRITICAL (1)" in out and "FAIL: critical=1" in out


def test_each_run_gets_its_own_run_id(con: duckdb.DuckDBPyConnection) -> None:
    load_clean(con)
    run.main([], con=con, cfg=CFG, today=TODAY)
    run.main([], con=con, cfg=CFG, today=TODAY)
    assert len({r[0] for r in dq_rows(con)}) == 2


def test_main_no_save_writes_nothing(con: duckdb.DuckDBPyConnection) -> None:
    load_clean(con)
    assert run.main(["--no-save"], con=con, cfg=CFG, today=TODAY) == 0
    assert dq_rows(con) == []


def test_report_never_prints_api_keys(
    con: duckdb.DuckDBPyConnection,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "SYNTHETIC-not-a-real-key-1234567890"
    monkeypatch.setenv("FRED_API_KEY", secret)
    monkeypatch.setenv("TWELVEDATA_API_KEY", secret)
    load_clean(con)
    upsert_macro(con, [MacroObservation("DGS10", date(2024, 11, 12), None)])
    run.main([], con=con, cfg=CFG, today=TODAY)
    assert secret not in capsys.readouterr().out
    details = " ".join(str(r) for r in con.execute("SELECT * FROM dq_results").fetchall())
    assert secret not in details


def test_insert_dq_results_rejects_unknown_severity(con: duckdb.DuckDBPyConnection) -> None:
    with pytest.raises(ValueError, match="severity"):
        insert_dq_results(con, "r", [Finding("x", "y", "fatal", 1, "")])
