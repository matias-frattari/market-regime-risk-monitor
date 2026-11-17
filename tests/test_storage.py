# SYNTHETIC — not real market data. All prices/values below are made-up round numbers
# used only to exercise the storage layer.
import time
from datetime import date, timedelta
from pathlib import Path

import duckdb
import pytest

from mrm.config import load_universe
from mrm.storage import db
from mrm.storage.loaders import (
    last_macro_date,
    last_price_date,
    log_ingestion,
    macro_fetch_start,
    replace_prices,
    upsert_assets,
    upsert_macro,
    upsert_prices,
)
from mrm.storage.models import MacroObservation, PriceBar
from mrm.storage.schema import TABLES, create_schema

D0 = date(2020, 1, 1)


def synthetic_bars(ticker: str, n: int, start: date = D0, close: float = 100.0) -> list[PriceBar]:
    """SYNTHETIC — consecutive calendar days with an obviously fake flat price."""
    return [
        PriceBar(ticker, start + timedelta(days=i), close, close, close, close, 1000)
        for i in range(n)
    ]


def count(con: duckdb.DuckDBPyConnection, table: str, where: str = "true") -> int:
    return con.execute(f"SELECT count(*) FROM {table} WHERE {where}").fetchone()[0]


# --- schema / connection ---------------------------------------------------------------


def test_create_schema_twice_is_idempotent(con: duckdb.DuckDBPyConnection) -> None:
    create_schema(con)  # second time (fixture already ran it once)
    tables = {
        r[0] for r in con.execute("SELECT table_name FROM information_schema.tables").fetchall()
    }
    assert set(TABLES) <= tables


def test_get_db_path_env_and_default(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv(db.DB_PATH_ENV, raising=False)
    assert db.get_db_path() == str(db.DEFAULT_DB_PATH)
    custom = tmp_path / "sub" / "w.duckdb"
    monkeypatch.setenv(db.DB_PATH_ENV, str(custom))
    assert db.get_db_path() == str(custom)
    db.connect().close()
    assert custom.exists()


# --- dim_asset ---------------------------------------------------------------------------


def test_upsert_assets_from_universe_is_idempotent(con: duckdb.DuckDBPyConnection) -> None:
    assets = load_universe()
    upsert_assets(con, assets)
    upsert_assets(con, assets)
    assert count(con, "dim_asset") == 13


# --- prices ------------------------------------------------------------------------------


def test_upsert_prices_does_not_duplicate(con: duckdb.DuckDBPyConnection) -> None:
    bars = synthetic_bars("SPY", 5)
    assert upsert_prices(con, bars) == 5
    assert upsert_prices(con, bars) == 5
    assert count(con, "raw_prices") == 5


def test_upsert_prices_dedupes_within_batch(con: duckdb.DuckDBPyConnection) -> None:
    bars = synthetic_bars("SPY", 3) + synthetic_bars("SPY", 1, close=999.0)
    assert upsert_prices(con, bars) == 3
    assert (
        con.execute("SELECT close FROM raw_prices WHERE trade_date = ?", [D0]).fetchone()[0]
        == 999.0
    )


def test_upsert_prices_readjustment_replaces_old_values(con: duckdb.DuckDBPyConnection) -> None:
    """A dividend re-adjusts the whole history: old dates come back with new closes."""
    upsert_prices(con, synthetic_bars("SPY", 10, close=100.0))
    upsert_prices(con, synthetic_bars("SPY", 10, close=90.0))
    assert count(con, "raw_prices") == 10
    closes = {r[0] for r in con.execute("SELECT DISTINCT close FROM raw_prices").fetchall()}
    assert closes == {90.0}


def test_replace_prices_drops_rows_outside_new_window(con: duckdb.DuckDBPyConnection) -> None:
    replace_prices(con, "SPY", synthetic_bars("SPY", 10))  # D0 .. D0+9
    replace_prices(con, "QQQ", synthetic_bars("QQQ", 4))
    shifted = synthetic_bars("SPY", 5, start=D0 + timedelta(days=7), close=80.0)  # D0+7 .. +11
    assert replace_prices(con, "SPY", shifted) == 5

    dates = [
        r[0]
        for r in con.execute(
            "SELECT trade_date FROM raw_prices WHERE ticker = 'SPY' ORDER BY 1"
        ).fetchall()
    ]
    assert dates == [b.trade_date for b in shifted]
    assert count(con, "raw_prices", "ticker = 'SPY' AND close <> 80.0") == 0
    assert count(con, "raw_prices", "ticker = 'QQQ'") == 4  # other tickers untouched


def test_replace_prices_empty_keeps_existing_rows(con: duckdb.DuckDBPyConnection) -> None:
    replace_prices(con, "SPY", synthetic_bars("SPY", 6))
    assert replace_prices(con, "SPY", []) == 0
    assert count(con, "raw_prices", "ticker = 'SPY'") == 6


def test_replace_prices_rejects_other_tickers(con: duckdb.DuckDBPyConnection) -> None:
    replace_prices(con, "SPY", synthetic_bars("SPY", 3))
    with pytest.raises(ValueError):
        replace_prices(con, "SPY", synthetic_bars("QQQ", 2))
    assert count(con, "raw_prices", "ticker = 'SPY'") == 3


def test_last_price_date(con: duckdb.DuckDBPyConnection) -> None:
    assert last_price_date(con, "SPY") is None
    upsert_prices(con, synthetic_bars("SPY", 10) + synthetic_bars("QQQ", 3))
    assert last_price_date(con, "SPY") == D0 + timedelta(days=9)
    assert last_price_date(con, "QQQ") == D0 + timedelta(days=2)


# --- macro -------------------------------------------------------------------------------


def synthetic_macro(series_id: str, n: int, value: float | None = 1.0) -> list[MacroObservation]:
    """SYNTHETIC — fake macro observations."""
    return [MacroObservation(series_id, D0 + timedelta(days=i), value) for i in range(n)]


def test_upsert_macro_does_not_duplicate_and_revises(con: duckdb.DuckDBPyConnection) -> None:
    upsert_macro(con, synthetic_macro("DGS10", 5))
    upsert_macro(con, synthetic_macro("DGS10", 5, value=2.0))  # revision
    assert count(con, "raw_macro") == 5
    assert count(con, "raw_macro", "value = 2.0") == 5


def test_upsert_macro_accepts_missing_values(con: duckdb.DuckDBPyConnection) -> None:
    upsert_macro(con, synthetic_macro("VIXCLS", 3, value=None))
    assert count(con, "raw_macro", "value IS NULL") == 3
    assert last_macro_date(con, "VIXCLS") == D0 + timedelta(days=2)


def test_last_macro_date_and_fetch_start(con: duckdb.DuckDBPyConnection) -> None:
    assert last_macro_date(con, "DGS10") is None
    assert macro_fetch_start(con, "DGS10") is None
    upsert_macro(con, synthetic_macro("DGS10", 100) + synthetic_macro("UNRATE", 2))
    last = D0 + timedelta(days=99)
    assert last_macro_date(con, "DGS10") == last
    assert macro_fetch_start(con, "DGS10") == last - timedelta(days=90)
    assert macro_fetch_start(con, "DGS10", overlap_days=0) == last
    assert last_macro_date(con, "UNRATE") == D0 + timedelta(days=1)


# --- ingest_log --------------------------------------------------------------------------


def test_log_ingestion_ids_increase(con: duckdb.DuckDBPyConnection) -> None:
    first = log_ingestion(con, "twelvedata", "SPY", D0, D0, 1, "success")
    second = log_ingestion(con, "fred", "DGS10", None, None, 0, "error", "HTTP 500")
    assert second > first
    assert count(con, "ingest_log") == 2


def test_log_ingestion_rejects_invalid_status(con: duckdb.DuckDBPyConnection) -> None:
    with pytest.raises(ValueError):
        log_ingestion(con, "fred", "DGS10", None, None, 0, "ok")


def test_timestamps_are_utc(con: duckdb.DuckDBPyConnection) -> None:
    from datetime import UTC, datetime

    log_ingestion(con, "fred", "DGS10", None, None, 0, "no_data")
    upsert_prices(con, synthetic_bars("SPY", 1))
    now_utc = datetime.now(UTC).replace(tzinfo=None)
    for sql in ("SELECT logged_at FROM ingest_log", "SELECT ingested_at FROM raw_prices"):
        stored = con.execute(sql).fetchone()[0]
        assert abs((now_utc - stored).total_seconds()) < 60


# --- performance (informational, not asserted) -------------------------------------------


def test_benchmark_replace_prices_65k_rows(con: duckdb.DuckDBPyConnection) -> None:
    """13 tickers x 5000 bars, SYNTHETIC. Prints timing; run with `pytest -s` to see it."""
    start = time.perf_counter()
    for i in range(13):
        replace_prices(con, f"T{i:02d}", synthetic_bars(f"T{i:02d}", 5000))
    elapsed = time.perf_counter() - start
    print(f"\n[benchmark] replace_prices 13 x 5000 = 65,000 rows: {elapsed:.2f} s")
    assert count(con, "raw_prices") == 65_000
