# SYNTHETIC — not real market data. Prices, yields and index values are made-up round numbers
# on real calendar dates, chosen only to exercise the calendar, publication-lag and
# forward-fill rules of macro_asof. Synthetic series ids start with SYN_.
from datetime import date, timedelta

import duckdb
import pytest

from mrm.config import load_macro_lags
from mrm.storage.loaders import sync_macro_lags, upsert_macro, upsert_prices
from mrm.storage.models import MacroLag, MacroObservation, PriceBar, ReleaseOverride
from mrm.storage.schema import TABLES, VIEWS
from tests.legacy_macro_asof import DAILY_SERIES, current_daily_rows, legacy_rows

REPO_LAGS = load_macro_lags()
ALL_SERIES = tuple(lag.series_id for lag in REPO_LAGS)


@pytest.fixture(autouse=True)
def repo_lags(con: duckdb.DuckDBPyConnection) -> None:
    """macro_asof exposes the series of macro_publication_lags: sync the repo config."""
    sync_macro_lags(con, REPO_LAGS)


# NYSE trading days around Veterans Day (11-11, NYSE open, bond market closed) and
# Thanksgiving (11-27, NYSE closed).
NOV = [date(2025, 11, d) for d in (7, 10, 11, 12, 13, 24, 25, 26, 28)]


def load_calendar(con: duckdb.DuckDBPyConnection, days: list[date], ticker: str = "SPY") -> None:
    """SYNTHETIC — one flat bar per trading day."""
    upsert_prices(con, [PriceBar(ticker, d, 100.0, 100.0, 100.0, 100.0, 1000) for d in days])


def load_macro(
    con: duckdb.DuckDBPyConnection, values: dict[date, float | None], series: str = "DGS10"
) -> None:
    """SYNTHETIC — None stands for FRED's '.' (stored as NULL)."""
    upsert_macro(con, [MacroObservation(series, d, v) for d, v in values.items()])


def asof(con: duckdb.DuckDBPyConnection, series: str = "DGS10") -> dict[date, tuple]:
    rows = con.execute(
        "SELECT trade_date, value, value_date, is_filled FROM macro_asof "
        "WHERE series_id = ? ORDER BY trade_date",
        [series],
    ).fetchall()
    return {r[0]: r[1:] for r in rows}


def test_view_registered(con: duckdb.DuckDBPyConnection) -> None:
    assert "macro_asof" in VIEWS
    names = {
        r[0]
        for r in con.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_type = 'VIEW'"
        ).fetchall()
    }
    assert "macro_asof" in names


def test_one_row_per_trading_day_and_series(con: duckdb.DuckDBPyConnection) -> None:
    # Calendar is the union of trade dates across tickers.
    load_calendar(con, NOV[:5], "SPY")
    load_calendar(con, NOV[3:], "TLT")
    rows = con.execute(
        "SELECT series_id, count(*), count(DISTINCT trade_date) FROM macro_asof GROUP BY 1"
    ).fetchall()
    assert sorted(rows) == sorted((s, len(NOV), len(NOV)) for s in ALL_SERIES)
    assert {r[0] for r in con.execute("SELECT trade_date FROM macro_asof").fetchall()} == set(NOV)


def test_strict_no_look_ahead(con: duckdb.DuckDBPyConnection) -> None:
    load_calendar(con, NOV[:3])
    load_macro(con, {date(2025, 11, 6): 4.0, NOV[0]: 4.1, NOV[1]: 4.2, NOV[2]: 4.3})
    got = asof(con)
    # Day d uses the observation of the previous trading day, never its own.
    assert got[NOV[0]] == (4.0, date(2025, 11, 6), False)
    assert got[NOV[1]] == (4.1, NOV[0], False)
    assert got[NOV[2]] == (4.2, NOV[1], False)


def test_later_observations_do_not_change_earlier_rows(con: duckdb.DuckDBPyConnection) -> None:
    load_calendar(con, NOV[:4])
    load_macro(con, {date(2025, 11, 6): 4.0, NOV[0]: 4.1})
    before = asof(con)
    load_macro(con, {NOV[1]: 9.9, NOV[2]: 9.8, NOV[3]: 9.7})
    after = asof(con)
    assert after[NOV[0]] == before[NOV[0]]
    assert after[NOV[1]] == before[NOV[1]]


def test_bond_holiday_on_trading_day_is_forward_filled(con: duckdb.DuckDBPyConnection) -> None:
    # Veterans Day 2025-11-11: NYSE open, FRED '.'.
    load_calendar(con, NOV[:5])
    load_macro(con, {NOV[0]: 4.1, NOV[1]: 4.2, NOV[2]: None, NOV[3]: 4.4, NOV[4]: 4.5})
    got = asof(con)
    assert got[NOV[2]] == (4.2, NOV[1], False)  # uses Monday 11-10, observed
    assert got[NOV[3]] == (4.2, NOV[1], True)  # 11-11 was '.', Monday's value carried
    assert got[NOV[4]] == (4.4, NOV[3], False)


def test_nyse_holiday_is_not_in_calendar(con: duckdb.DuckDBPyConnection) -> None:
    # Thanksgiving 2025-11-27: NYSE and bond market closed, FRED '.'.
    load_calendar(con, NOV[5:])
    load_macro(con, {NOV[6]: 4.0, NOV[7]: 4.1, date(2025, 11, 27): None, NOV[8]: 4.2})
    got = asof(con)
    assert date(2025, 11, 27) not in got
    assert got[NOV[8]] == (4.1, NOV[7], False)  # previous trading day observed


def test_observation_on_nyse_closed_day_is_used(con: duckdb.DuckDBPyConnection) -> None:
    # 2025-01-09: NYSE closed (national day of mourning), bond market open, FRED has a value.
    days = [date(2025, 1, 7), date(2025, 1, 8), date(2025, 1, 10), date(2025, 1, 13)]
    load_calendar(con, days)
    load_macro(
        con, {date(2025, 1, 7): 4.6, date(2025, 1, 8): 4.7, date(2025, 1, 9): 4.8, days[2]: 4.9}
    )
    got = asof(con)
    assert date(2025, 1, 9) not in got
    assert got[days[2]] == (4.8, date(2025, 1, 9), False)
    assert got[days[3]] == (4.9, days[2], False)


def test_series_start_is_null_not_imputed(con: duckdb.DuckDBPyConnection) -> None:
    load_calendar(con, NOV[:4])
    load_macro(con, {NOV[1]: 4.2, NOV[2]: 4.3}, series="DGS2")
    got = asof(con, "DGS2")
    assert got[NOV[0]] == (None, None, False)
    assert got[NOV[1]] == (None, None, False)  # its own first obs is not known yet
    assert got[NOV[2]] == (4.2, NOV[1], False)
    # A series with no data at all stays NULL on every day.
    assert all(v == (None, None, False) for v in asof(con, "DGS3MO").values())


def test_trading_day_without_fred_row_is_forward_filled(con: duckdb.DuckDBPyConnection) -> None:
    # No FRED row for 11-11 and 11-12 (e.g. not published yet): both following days carry 11-10.
    load_calendar(con, NOV[:5])
    load_macro(con, {NOV[0]: 4.1, NOV[1]: 4.2})
    got = asof(con)
    assert got[NOV[2]] == (4.2, NOV[1], False)
    assert got[NOV[3]] == (4.2, NOV[1], True)
    assert got[NOV[4]] == (4.2, NOV[1], True)


def test_all_configured_series_present_and_raw_untouched(
    con: duckdb.DuckDBPyConnection,
) -> None:
    load_calendar(con, NOV[:3])
    for series in ("CPIAUCSL", "NFCI", "UNRATE", "SYN_NOT_CONFIGURED"):
        load_macro(con, {NOV[0]: 1.0}, series=series)
    raw_before = con.execute("SELECT * FROM raw_macro ORDER BY ALL").fetchall()
    series = {r[0] for r in con.execute("SELECT DISTINCT series_id FROM macro_asof").fetchall()}
    assert series == set(ALL_SERIES)  # unconfigured raw series never reach macro_asof
    assert con.execute("SELECT * FROM raw_macro ORDER BY ALL").fetchall() == raw_before


def test_without_synced_lags_the_view_is_empty(con: duckdb.DuckDBPyConnection) -> None:
    load_calendar(con, NOV[:3])
    load_macro(con, {NOV[0]: 4.0})
    sync_macro_lags(con, [])
    assert con.execute("SELECT count(*) FROM macro_asof").fetchone()[0] == 0
    assert {"macro_publication_lags", "macro_release_overrides"} <= set(TABLES)


# --- non-regression: daily series identical to the Phase 1.6 view --------------------------


def test_daily_series_identical_to_phase_1_6_view(con: duckdb.DuckDBPyConnection) -> None:
    # Two tickers with different calendars, a NYSE-closed obs (01-09), FRED '.', a missing row,
    # a late-starting series (DGS2) and one with no data (DGS3MO).
    days = [date(2025, 1, 7), date(2025, 1, 8), date(2025, 1, 10), date(2025, 1, 13)]
    load_calendar(con, days + NOV[:6], "SPY")
    load_calendar(con, NOV[4:], "TLT")
    load_macro(con, {date(2025, 1, 7): 4.6, date(2025, 1, 9): 4.8, days[2]: None})
    load_macro(con, {NOV[0]: 4.1, NOV[1]: 4.2, NOV[2]: None, NOV[4]: 4.5, NOV[8]: 4.9})
    load_macro(con, {NOV[1]: 3.9, NOV[2]: 3.8, NOV[6]: None, NOV[7]: 3.7}, series="DGS2")
    load_macro(con, {d: -0.1 * i for i, d in enumerate(NOV)}, series="T10Y2Y")
    for series in ("CPIAUCSL", "NFCI"):  # lagged series must not disturb the daily rows
        load_macro(con, {date(2025, 10, 1): 9.0, NOV[0]: 9.1}, series=series)
    legacy = legacy_rows(con)
    assert len(legacy) == len(DAILY_SERIES) * (len(days) + len(NOV))
    assert current_daily_rows(con) == legacy  # exact equality, values included


def test_daily_series_new_columns(con: duckdb.DuckDBPyConnection) -> None:
    load_calendar(con, NOV[:5])
    load_macro(con, {NOV[0]: 4.1, NOV[1]: 4.2, NOV[2]: None})
    rows = con.execute(
        "SELECT trade_date, value_date, available_date, age_days FROM macro_asof "
        "WHERE series_id = 'DGS10' ORDER BY trade_date"
    ).fetchall()
    assert rows[0] == (NOV[0], None, None, None)
    for trade_date, value_date, available_date, age_days in rows[1:]:
        assert available_date == value_date  # lag 0
        assert age_days == (trade_date - value_date).days
    assert rows[4][3] == 3  # 11-13 carries Monday 11-10


# --- lagged series (synthetic SYN_M monthly, SYN_W weekly) ----------------------------------

# SYNTHETIC calendar: every weekday of 2025-09-01..2025-12-31 (holidays ignored on purpose).
WEEKDAYS = [
    date(2025, 9, 1) + timedelta(days=i)
    for i in range(122)
    if (date(2025, 9, 1) + timedelta(days=i)).weekday() < 5
]


def _lag(
    series_id: str, frequency: str, lag_days: int, overrides: dict[date, date] | None = None
) -> MacroLag:
    return MacroLag(
        series_id,
        frequency,
        lag_days,
        "synthetic",
        tuple(
            ReleaseOverride(series_id, o, a, "synthetic", "synthetic")
            for o, a in (overrides or {}).items()
        ),
    )


def full(con: duckdb.DuckDBPyConnection, series: str) -> dict[date, tuple]:
    rows = con.execute(
        "SELECT trade_date, value, value_date, is_filled, available_date, age_days "
        "FROM macro_asof WHERE series_id = ? ORDER BY trade_date",
        [series],
    ).fetchall()
    return {r[0]: r[1:] for r in rows}


@pytest.fixture
def lagged(con: duckdb.DuckDBPyConnection) -> duckdb.DuckDBPyConnection:
    load_calendar(con, WEEKDAYS)
    sync_macro_lags(
        con,
        [
            _lag("SYN_M", "monthly", 10),
            # Week ending 10-17 released late, together with the week ending 10-24.
            _lag("SYN_W", "weekly", 5, {date(2025, 10, 17): date(2025, 10, 29)}),
        ],
    )
    # SYN_M: obs on the 1st of the month (reference month), available 10 days later.
    load_macro(con, {date(2025, 10, 1): 1.0, date(2025, 11, 1): 2.0}, series="SYN_M")
    # SYN_W: obs on Fridays (end of the reference week), available the next Wednesday.
    weekly = {date(2025, 10, 3): 10.0, date(2025, 10, 10): 11.0, date(2025, 10, 17): 12.0}
    load_macro(con, weekly | {date(2025, 10, 24): 13.0}, series="SYN_W")
    return con


def test_monthly_value_available_the_day_after_obs_plus_lag(
    lagged: duckdb.DuckDBPyConnection,
) -> None:
    got = full(lagged, "SYN_M")
    # 2025-10-01 + 10 = Sat 10-11: first usable trading day is Mon 10-13.
    assert got[date(2025, 10, 10)] == (None, None, False, None, None)  # series start: NULL
    assert got[date(2025, 10, 13)] == (1.0, date(2025, 10, 1), False, date(2025, 10, 11), 12)
    assert got[date(2025, 10, 14)] == (1.0, date(2025, 10, 1), True, date(2025, 10, 11), 13)
    # 2025-11-01 + 10 = Tue 11-11: still the October value on 11-11, November from 11-12.
    assert got[date(2025, 11, 11)] == (1.0, date(2025, 10, 1), True, date(2025, 10, 11), 41)
    assert got[date(2025, 11, 12)] == (2.0, date(2025, 11, 1), False, date(2025, 11, 11), 11)
    assert got[date(2025, 11, 13)][2] is True
    assert got[date(2025, 12, 31)] == (2.0, date(2025, 11, 1), True, date(2025, 11, 11), 60)
    assert all(v[0] is None for d, v in got.items() if d <= date(2025, 10, 11))


def test_weekly_value_and_delayed_release_override(lagged: duckdb.DuckDBPyConnection) -> None:
    got = full(lagged, "SYN_W")
    # Week ending Fri 10-03 is released Wed 10-08 and usable from Thu 10-09.
    assert got[date(2025, 10, 8)][0] is None
    assert got[date(2025, 10, 9)] == (10.0, date(2025, 10, 3), False, date(2025, 10, 8), 6)
    assert got[date(2025, 10, 15)][:2] == (10.0, date(2025, 10, 3))
    assert got[date(2025, 10, 16)][:2] == (11.0, date(2025, 10, 10))
    # Override: 10-17 not released on 10-22 but on 10-29, the same day as 10-24 (+5).
    assert got[date(2025, 10, 23)][:2] == (11.0, date(2025, 10, 10))
    assert got[date(2025, 10, 29)][:2] == (11.0, date(2025, 10, 10))
    # Both released 10-29: the latest reference week wins; 10-17 is never the latest known.
    assert got[date(2025, 10, 30)] == (13.0, date(2025, 10, 24), False, date(2025, 10, 29), 6)
    assert all(v[1] != date(2025, 10, 17) for v in got.values())
    assert got[date(2025, 10, 31)][2:4] == (True, date(2025, 10, 29))


def test_override_never_lets_a_newer_value_jump_ahead(con: duckdb.DuckDBPyConnection) -> None:
    # Older month delayed past the newer month's normal release: available_date is a running
    # max, so the newer value waits too (conservative, never earlier than its real release).
    load_calendar(con, WEEKDAYS)
    sync_macro_lags(con, [_lag("SYN_M", "monthly", 10, {date(2025, 10, 1): date(2025, 11, 20)})])
    values = {date(2025, 9, 1): 0.5, date(2025, 10, 1): 1.0, date(2025, 11, 1): 2.0}
    load_macro(con, values, series="SYN_M")
    got = full(con, "SYN_M")
    assert got[date(2025, 11, 12)][:2] == (0.5, date(2025, 9, 1))
    assert got[date(2025, 11, 20)][:2] == (0.5, date(2025, 9, 1))
    assert got[date(2025, 11, 21)] == (2.0, date(2025, 11, 1), False, date(2025, 11, 20), 20)
    assert all(v[1] != date(2025, 10, 1) for v in got.values())


@pytest.mark.parametrize(
    ("series", "t"), [("SYN_M", date(2025, 11, 11)), ("SYN_W", date(2025, 10, 29))]
)
def test_data_not_yet_available_at_t_does_not_change_t(
    lagged: duckdb.DuckDBPyConnection, series: str, t: date
) -> None:
    before = full(lagged, series)
    # Revise every observation not yet usable at t (obs_date before t included: their release
    # date is >= t), and add new observations after t.
    late = lagged.execute(
        """
        SELECT m.obs_date
        FROM raw_macro m
        JOIN macro_publication_lags l USING (series_id)
        LEFT JOIN macro_release_overrides o USING (series_id, obs_date)
        WHERE m.series_id = ? AND coalesce(o.available_date, m.obs_date + l.lag_days) >= ?
        """,
        [series, t],
    ).fetchall()
    assert any(r[0] < t for r in late)  # includes values dated before t, released later
    changed = {r[0]: 99.0 for r in late}
    load_macro(lagged, changed | {t: 98.0, t + timedelta(days=1): 97.0}, series=series)
    after = full(lagged, series)
    assert {d: v for d, v in after.items() if d <= t} == {d: v for d, v in before.items() if d <= t}
    assert after != before  # the changes do show up after t


def test_monthly_gap_keeps_last_value_and_age_grows(con: duckdb.DuckDBPyConnection) -> None:
    # A month never published (FRED '.', like UNRATE 2025-10): the older value is carried and
    # age_days exposes the staleness.
    load_calendar(con, WEEKDAYS)
    sync_macro_lags(con, [_lag("SYN_M", "monthly", 10)])
    values = {date(2025, 9, 1): 4.4, date(2025, 10, 1): None, date(2025, 11, 1): 4.6}
    load_macro(con, values, series="SYN_M")
    got = full(con, "SYN_M")
    assert got[date(2025, 11, 11)] == (4.4, date(2025, 9, 1), True, date(2025, 9, 11), 71)
    assert got[date(2025, 11, 12)][:2] == (4.6, date(2025, 11, 1))
