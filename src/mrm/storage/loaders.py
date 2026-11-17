"""Idempotent loaders and incremental-load helpers for the warehouse.

Prices from Twelve Data (``adjust=all``) are re-adjusted over the whole history after
every dividend/split, so ingestion reloads each ticker in full with ``replace_prices``.
Macro series (FRED) can be revised, so they are re-fetched with an overlap window
(``macro_fetch_start``) and upserted.
"""

from collections.abc import Callable, Hashable, Iterable, Sequence
from datetime import UTC, date, datetime, timedelta
from typing import Any

import duckdb

from mrm.storage.models import Asset, MacroObservation, PriceBar

PRICE_SOURCE = "twelvedata"
MACRO_SOURCE = "fred"
LOG_STATUSES: frozenset[str] = frozenset({"success", "no_data", "error"})
DEFAULT_MACRO_OVERLAP_DAYS = 90

# Bulk writes pass each column as one list parameter and expand it with unnest(): one
# statement per batch instead of one per row (~40x faster than executemany on 65k rows).
_INSERT_PRICE = """
    INSERT INTO raw_prices
        (ticker, trade_date, open, high, low, close, volume, source, ingested_at)
    SELECT unnest(?::VARCHAR[]), unnest(?::DATE[]), unnest(?::DOUBLE[]), unnest(?::DOUBLE[]),
           unnest(?::DOUBLE[]), unnest(?::DOUBLE[]), unnest(?::BIGINT[]), ?, ?
"""
_UPSERT_PRICE = (
    _INSERT_PRICE
    + """
    ON CONFLICT (ticker, trade_date) DO UPDATE SET
        open = excluded.open, high = excluded.high, low = excluded.low,
        close = excluded.close, volume = excluded.volume,
        source = excluded.source, ingested_at = excluded.ingested_at
"""
)
_UPSERT_MACRO = """
    INSERT INTO raw_macro (series_id, obs_date, value, source, ingested_at)
    SELECT unnest(?::VARCHAR[]), unnest(?::DATE[]), unnest(?::DOUBLE[]), ?, ?
    ON CONFLICT (series_id, obs_date) DO UPDATE SET
        value = excluded.value, source = excluded.source, ingested_at = excluded.ingested_at
"""
_UPSERT_ASSET = """
    INSERT INTO dim_asset (ticker, asset_class, updated_at) VALUES (?, ?, ?)
    ON CONFLICT (ticker) DO UPDATE SET
        asset_class = excluded.asset_class, updated_at = excluded.updated_at
"""


def utc_now() -> datetime:
    """Current time as a naive UTC datetime (stored in TIMESTAMP columns)."""
    return datetime.now(UTC).replace(tzinfo=None)


def _dedupe[T](rows: Iterable[T], key: Callable[[T], Hashable]) -> list[T]:
    """Drop rows with a repeated primary key; the last occurrence wins."""
    return list({key(r): r for r in rows}.values())


# A step is (sql, params, many): many=True runs executemany over a list of row tuples.
_Step = tuple[str, Sequence[Any], bool]


def _run_in_transaction(con: duckdb.DuckDBPyConnection, steps: Sequence[_Step]) -> None:
    """Run all steps atomically; roll back on any error."""
    con.begin()
    try:
        for sql, params, many in steps:
            if not many:
                con.execute(sql, params)
            elif params:
                con.executemany(sql, params)
        con.commit()
    except Exception:
        con.rollback()
        raise


def _price_params(bars: list[PriceBar], source: str) -> list[Any]:
    """Column-wise parameters for _INSERT_PRICE / _UPSERT_PRICE."""
    return [
        [b.ticker for b in bars],
        [b.trade_date for b in bars],
        [b.open for b in bars],
        [b.high for b in bars],
        [b.low for b in bars],
        [b.close for b in bars],
        [b.volume for b in bars],
        source,
        utc_now(),
    ]


def upsert_assets(con: duckdb.DuckDBPyConnection, assets: Iterable[Asset]) -> int:
    """Insert or update dim_asset rows. Returns the number of distinct tickers written."""
    rows = _dedupe(assets, key=lambda a: a.ticker)
    now = utc_now()
    _run_in_transaction(
        con, [(_UPSERT_ASSET, [(a.ticker, a.asset_class, now) for a in rows], True)]
    )
    return len(rows)


def sync_assets(con: duckdb.DuckDBPyConnection, assets: Iterable[Asset]) -> int:
    """Make dim_asset equal to ``assets``: delete tickers no longer present, upsert the rest,
    in one transaction. Raw prices and ingest_log rows of removed tickers are kept.
    Raises ValueError on an empty universe (never wipes the table). Returns tickers written.
    """
    rows = _dedupe(assets, key=lambda a: a.ticker)
    if not rows:
        raise ValueError("sync_assets needs a non-empty universe")
    now = utc_now()
    _run_in_transaction(
        con,
        [
            (
                "DELETE FROM dim_asset WHERE NOT list_contains(?::VARCHAR[], ticker)",
                [[a.ticker for a in rows]],
                False,
            ),
            (_UPSERT_ASSET, [(a.ticker, a.asset_class, now) for a in rows], True),
        ],
    )
    return len(rows)


def upsert_prices(
    con: duckdb.DuckDBPyConnection, bars: Iterable[PriceBar], source: str = PRICE_SOURCE
) -> int:
    """Insert or update price bars by (ticker, trade_date). Generic use; ingestion uses
    ``replace_prices``. Returns the number of distinct rows written."""
    rows = _dedupe(bars, key=lambda b: (b.ticker, b.trade_date))
    if rows:
        _run_in_transaction(con, [(_UPSERT_PRICE, _price_params(rows, source), False)])
    return len(rows)


def replace_prices(
    con: duckdb.DuckDBPyConnection,
    ticker: str,
    bars: Iterable[PriceBar],
    source: str = PRICE_SOURCE,
) -> int:
    """Replace the full stored history of ``ticker`` with ``bars`` in one transaction.

    Needed because adjusted prices are rewritten over the whole history after each
    dividend/split, and the provider's window moves forward. If ``bars`` is empty nothing
    is deleted (guards against empty or failed responses). Returns rows inserted.
    """
    rows = _dedupe(bars, key=lambda b: b.trade_date)
    if not rows:
        return 0
    foreign = {b.ticker for b in rows if b.ticker != ticker}
    if foreign:
        raise ValueError(f"replace_prices({ticker!r}) got bars for other tickers: {foreign}")
    _run_in_transaction(
        con,
        [
            ("DELETE FROM raw_prices WHERE ticker = ?", [ticker], False),
            (_INSERT_PRICE, _price_params(rows, source), False),
        ],
    )
    return len(rows)


def upsert_macro(
    con: duckdb.DuckDBPyConnection,
    observations: Iterable[MacroObservation],
    source: str = MACRO_SOURCE,
) -> int:
    """Insert or update macro observations by (series_id, obs_date). Returns rows written."""
    rows = _dedupe(observations, key=lambda o: (o.series_id, o.obs_date))
    if rows:
        params = [
            [o.series_id for o in rows],
            [o.obs_date for o in rows],
            [o.value for o in rows],
            source,
            utc_now(),
        ]
        _run_in_transaction(con, [(_UPSERT_MACRO, params, False)])
    return len(rows)


def last_price_date(con: duckdb.DuckDBPyConnection, ticker: str) -> date | None:
    """Latest stored trade_date for ``ticker`` (diagnostics/logging only), or None."""
    row = con.execute("SELECT max(trade_date) FROM raw_prices WHERE ticker = ?", [ticker])
    return row.fetchone()[0]


def last_macro_date(con: duckdb.DuckDBPyConnection, series_id: str) -> date | None:
    """Latest stored obs_date for ``series_id`` (NULL values included), or None."""
    row = con.execute("SELECT max(obs_date) FROM raw_macro WHERE series_id = ?", [series_id])
    return row.fetchone()[0]


def macro_fetch_start(
    con: duckdb.DuckDBPyConnection,
    series_id: str,
    overlap_days: int = DEFAULT_MACRO_OVERLAP_DAYS,
) -> date | None:
    """First date to request from FRED: last stored date minus an overlap window that
    picks up revisions. None means no data yet (fetch the full history)."""
    if overlap_days < 0:
        raise ValueError("overlap_days must be >= 0")
    last = last_macro_date(con, series_id)
    return None if last is None else last - timedelta(days=overlap_days)


def log_ingestion(
    con: duckdb.DuckDBPyConnection,
    source: str,
    entity: str,
    date_from: date | None,
    date_to: date | None,
    rows_loaded: int,
    status: str,
    message: str | None = None,
) -> int:
    """Append one ingest_log row and return its log_id.

    ``message`` must never contain request URLs or API keys.
    """
    if status not in LOG_STATUSES:
        raise ValueError(f"invalid status {status!r}; expected one of {sorted(LOG_STATUSES)}")
    row = con.execute(
        """
        INSERT INTO ingest_log
            (source, entity, date_from, date_to, rows_loaded, status, message, logged_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        RETURNING log_id
        """,
        [source, entity, date_from, date_to, rows_loaded, status, message, utc_now()],
    )
    return int(row.fetchone()[0])
