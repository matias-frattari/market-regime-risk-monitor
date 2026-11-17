"""Daily price ingestion: provider -> validation -> full reload per ticker -> ingest_log.

CLI::

    uv run python -m mrm.ingest.prices [--tickers SPY,TLT] [--dry-run]
"""

import argparse
import os
import sys
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import date

import duckdb
from dotenv import load_dotenv

from mrm.config import load_universe
from mrm.ingest.providers import PriceProvider, ProviderError, RateLimitError, redact
from mrm.ingest.twelvedata import TwelveDataProvider
from mrm.storage.db import connect
from mrm.storage.loaders import log_ingestion, replace_prices, sync_assets
from mrm.storage.schema import create_schema

API_KEY_ENV = "TWELVEDATA_API_KEY"
PACE_SECONDS = 8.0  # free plan: 8 requests/minute
MAX_CONSECUTIVE_RATE_LIMITS = 2
ABORTED_MESSAGE = "aborted: rate limit"


@dataclass(frozen=True, slots=True)
class TickerResult:
    """Outcome of one ticker. ``message`` is safe to print and log."""

    ticker: str
    rows: int
    date_min: date | None
    date_max: date | None
    status: str
    message: str | None = None
    rate_limited: bool = False


def _fetch_and_load(
    con: duckdb.DuckDBPyConnection | None,
    provider: PriceProvider,
    ticker: str,
    dry_run: bool,
    secret: str | None,
) -> TickerResult:
    try:
        fetched = provider.fetch_daily(ticker)
        if not fetched.bars:
            return TickerResult(ticker, 0, None, None, "no_data", "empty response")
        rows = len(fetched.bars) if dry_run else replace_prices(con, ticker, fetched.bars)
        notes = []
        if fetched.dropped_null_close:
            notes.append(f"dropped {fetched.dropped_null_close} rows with null close")
        if fetched.dropped_partial_bar:
            notes.append("dropped partial bar")
        message = "; ".join(notes) or None
        return TickerResult(
            ticker,
            rows,
            fetched.bars[0].trade_date,
            fetched.bars[-1].trade_date,
            "success",
            message,
        )
    except RateLimitError as exc:
        return TickerResult(ticker, 0, None, None, "error", redact(str(exc), secret), True)
    except ProviderError as exc:
        return TickerResult(ticker, 0, None, None, "error", redact(str(exc), secret))
    except Exception as exc:  # one failing ticker must not stop the run
        return TickerResult(ticker, 0, None, None, "error", type(exc).__name__)


def _log(con: duckdb.DuckDBPyConnection, source: str, result: TickerResult) -> TickerResult:
    """Write the ingest_log row; a logging failure turns the result into an error."""
    try:
        log_ingestion(
            con,
            source,
            result.ticker,
            result.date_min,
            result.date_max,
            result.rows,
            result.status,
            result.message,
        )
        return result
    except Exception as exc:
        return TickerResult(
            result.ticker,
            result.rows,
            result.date_min,
            result.date_max,
            "error",
            f"ingest_log write failed: {type(exc).__name__}",
            result.rate_limited,
        )


def ingest_ticker(
    con: duckdb.DuckDBPyConnection | None,
    provider: PriceProvider,
    ticker: str,
    *,
    dry_run: bool = False,
    secret: str | None = None,
) -> TickerResult:
    """Fetch, validate and fully reload one ticker, then log the attempt.

    Never raises. On any failure nothing is deleted. ``dry_run`` touches no table
    (``con`` may then be None). ``secret`` is redacted from every message.
    """
    result = _fetch_and_load(con, provider, ticker, dry_run, secret)
    if dry_run or con is None:
        return result
    return _log(con, provider.name, result)


def ingest_prices(
    con: duckdb.DuckDBPyConnection | None,
    provider: PriceProvider,
    tickers: Sequence[str],
    *,
    pace_seconds: float = PACE_SECONDS,
    sleep: Callable[[float], None] = time.sleep,
    dry_run: bool = False,
    secret: str | None = None,
    on_result: Callable[[TickerResult], None] | None = None,
) -> list[TickerResult]:
    """Ingest ``tickers`` in order, waiting ``pace_seconds`` between requests.

    After ``MAX_CONSECUTIVE_RATE_LIMITS`` consecutive tickers fail on the rate limit (each
    already retried once), the run stops; remaining tickers are reported (and logged) as
    errors without calling the provider.
    """
    results: list[TickerResult] = []
    consecutive_limited = 0
    aborted = False
    for i, ticker in enumerate(tickers):
        if aborted:
            result = TickerResult(ticker, 0, None, None, "error", ABORTED_MESSAGE)
            if not dry_run and con is not None:
                result = _log(con, provider.name, result)
        else:
            if i > 0:
                sleep(pace_seconds)
            result = ingest_ticker(con, provider, ticker, dry_run=dry_run, secret=secret)
            consecutive_limited = consecutive_limited + 1 if result.rate_limited else 0
            aborted = consecutive_limited >= MAX_CONSECUTIVE_RATE_LIMITS
        results.append(result)
        if on_result is not None:
            on_result(result)
    return results


def format_summary(results: Sequence[TickerResult]) -> str:
    """Plain-text summary table (no prices)."""
    header = ("ticker", "rows", "date_min", "date_max", "status", "detail")
    rows = [
        (
            r.ticker,
            str(r.rows),
            r.date_min.isoformat() if r.date_min else "-",
            r.date_max.isoformat() if r.date_max else "-",
            r.status,
            r.message or "",
        )
        for r in results
    ]
    widths = [max(len(row[c]) for row in [header, *rows]) for c in range(len(header))]
    lines = ["  ".join(cell.ljust(w) for cell, w in zip(row, widths)).rstrip() for row in rows]
    head = "  ".join(cell.ljust(w) for cell, w in zip(header, widths)).rstrip()
    return "\n".join([head, "-" * len(head), *lines])


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m mrm.ingest.prices",
        description="Load full daily adjusted price history from Twelve Data into DuckDB.",
    )
    parser.add_argument(
        "--tickers", help="Comma-separated subset of config/universe.yaml (default: all)"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Fetch and validate (calls the API), but write nothing to the warehouse",
    )
    return parser.parse_args(argv)


def main(
    argv: Sequence[str] | None = None,
    *,
    provider: PriceProvider | None = None,
    con: duckdb.DuckDBPyConnection | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> int:
    """CLI entry point. ``provider``/``con``/``sleep`` are injectable for tests.

    Exit codes: 0 all tickers ok or no_data, 1 some ticker failed, 2 usage/config error.
    """
    args = _parse_args(argv)
    universe = load_universe()
    known = [a.ticker for a in universe]
    if args.tickers:
        tickers = [t.strip().upper() for t in args.tickers.split(",") if t.strip()]
        unknown = [t for t in tickers if t not in known]
        if unknown or not tickers:
            print(f"error: tickers not in config/universe.yaml: {', '.join(unknown) or '(none)'}")
            return 2
    else:
        tickers = known

    secret: str | None = None
    if provider is None:
        load_dotenv()
        secret = os.environ.get(API_KEY_ENV, "").strip() or None
        if secret is None:
            print(f"error: {API_KEY_ENV} is not set (see .env.example)")
            return 2
        provider = TwelveDataProvider(secret, sleep=sleep)

    own_con = False
    if con is None and not args.dry_run:
        con = connect()
        own_con = True
    try:
        if con is not None and not args.dry_run:
            create_schema(con)
            sync_assets(con, universe)
        mode = " (dry run: nothing is written)" if args.dry_run else ""
        print(f"Ingesting {len(tickers)} tickers from {provider.name}{mode}")

        def report(result: TickerResult) -> None:
            print(f"  {result.ticker}: {result.status} ({result.rows} rows)", flush=True)

        results = ingest_prices(
            con,
            provider,
            tickers,
            sleep=sleep,
            dry_run=args.dry_run,
            secret=secret,
            on_result=report,
        )
    finally:
        if own_con and con is not None:
            con.close()

    print()
    print(format_summary(results))
    return 1 if any(r.status == "error" for r in results) else 0


if __name__ == "__main__":
    sys.exit(main())
