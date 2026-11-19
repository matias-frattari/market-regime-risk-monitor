# SYNTHETIC — not real market data. Prices and yields are made-up round numbers on real
# calendar dates, chosen only to exercise the calendar and forward-fill rules of macro_asof.
from datetime import date

import duckdb

from mrm.storage.loaders import upsert_macro, upsert_prices
from mrm.storage.models import MacroObservation, PriceBar
from mrm.storage.schema import MACRO_ASOF_SERIES, VIEWS

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
    assert sorted(rows) == sorted((s, len(NOV), len(NOV)) for s in MACRO_ASOF_SERIES)
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


def test_out_of_scope_series_excluded_and_raw_untouched(con: duckdb.DuckDBPyConnection) -> None:
    load_calendar(con, NOV[:3])
    for series in ("CPIAUCSL", "NFCI", "UNRATE"):
        load_macro(con, {NOV[0]: 1.0}, series=series)
    raw_before = con.execute("SELECT * FROM raw_macro ORDER BY ALL").fetchall()
    series = {r[0] for r in con.execute("SELECT DISTINCT series_id FROM macro_asof").fetchall()}
    assert series == set(MACRO_ASOF_SERIES)
    assert con.execute("SELECT * FROM raw_macro ORDER BY ALL").fetchall() == raw_before
