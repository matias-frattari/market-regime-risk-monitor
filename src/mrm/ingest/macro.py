"""FRED macro ingestion: overlap window -> client -> validation -> upsert -> ingest_log.

CLI::

    uv run python -m mrm.ingest.macro [--series DGS10,NFCI] [--dry-run]
"""

import argparse
import os
import sys
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import date
from typing import Protocol

import duckdb
from dotenv import load_dotenv

from mrm.config import BLOCKED_SERIES, load_macro_series
from mrm.ingest.fred import FredClient, MacroFetchResult
from mrm.ingest.providers import ProviderError, redact
from mrm.storage.db import connect
from mrm.storage.loaders import log_ingestion, macro_fetch_start, upsert_macro
from mrm.storage.models import MacroSeries
from mrm.storage.schema import create_schema

API_KEY_ENV = "FRED_API_KEY"
PACE_SECONDS = 1.0  # FRED allows 120 requests/minute
FULL_RELOAD_NOTE = "full reload"


class MacroClient(Protocol):
    """Source of macro observations (``FredClient`` or a test double)."""

    name: str

    def fetch_observations(self, series_id: str, start: date | None) -> MacroFetchResult: ...


@dataclass(frozen=True, slots=True)
class SeriesResult:
    """Outcome of one series. ``message`` is safe to print and log."""

    series_id: str
    rows: int
    date_min: date | None
    date_max: date | None
    status: str
    message: str | None = None


def _fetch_start(con: duckdb.DuckDBPyConnection | None, series: MacroSeries) -> date | None:
    if series.overlap_days is None or con is None:
        return None
    return macro_fetch_start(con, series.series_id, series.overlap_days)


def _fetch_and_load(
    con: duckdb.DuckDBPyConnection | None,
    client: MacroClient,
    series: MacroSeries,
    dry_run: bool,
    secret: str | None,
) -> SeriesResult:
    sid = series.series_id
    try:
        fetched = client.fetch_observations(sid, _fetch_start(con, series))
        if not fetched.observations:
            return SeriesResult(sid, 0, None, None, "no_data", "empty response")
        obs = fetched.observations
        rows = len(obs) if dry_run else upsert_macro(con, obs)
        notes = []
        if fetched.missing:
            notes.append(f"{fetched.missing} missing values ('.')")
        if series.overlap_days is None:
            notes.append(FULL_RELOAD_NOTE)
        message = "; ".join(notes) or None
        return SeriesResult(sid, rows, obs[0].obs_date, obs[-1].obs_date, "success", message)
    except ProviderError as exc:
        return SeriesResult(sid, 0, None, None, "error", redact(str(exc), secret))
    except Exception as exc:  # one failing series must not stop the run
        return SeriesResult(sid, 0, None, None, "error", type(exc).__name__)


def _log(con: duckdb.DuckDBPyConnection, source: str, result: SeriesResult) -> SeriesResult:
    """Write the ingest_log row; a logging failure turns the result into an error."""
    try:
        log_ingestion(
            con,
            source,
            result.series_id,
            result.date_min,
            result.date_max,
            result.rows,
            result.status,
            result.message,
        )
        return result
    except Exception as exc:
        return SeriesResult(
            result.series_id,
            result.rows,
            result.date_min,
            result.date_max,
            "error",
            f"ingest_log write failed: {type(exc).__name__}",
        )


def ingest_series(
    con: duckdb.DuckDBPyConnection | None,
    client: MacroClient,
    series: MacroSeries,
    *,
    dry_run: bool = False,
    secret: str | None = None,
) -> SeriesResult:
    """Fetch one series from its overlap start (or full history), upsert it and log.

    Never raises. On any failure nothing is written to raw_macro. ``dry_run`` writes no
    table; without ``con`` (CLI dry run) the full history is requested.
    """
    result = _fetch_and_load(con, client, series, dry_run, secret)
    if dry_run or con is None:
        return result
    return _log(con, client.name, result)


def ingest_macro(
    con: duckdb.DuckDBPyConnection | None,
    client: MacroClient,
    series: Sequence[MacroSeries],
    *,
    pace_seconds: float = PACE_SECONDS,
    sleep: Callable[[float], None] = time.sleep,
    dry_run: bool = False,
    secret: str | None = None,
    on_result: Callable[[SeriesResult], None] | None = None,
) -> list[SeriesResult]:
    """Ingest ``series`` in order, waiting ``pace_seconds`` between requests."""
    results: list[SeriesResult] = []
    for i, item in enumerate(series):
        if i > 0:
            sleep(pace_seconds)
        result = ingest_series(con, client, item, dry_run=dry_run, secret=secret)
        results.append(result)
        if on_result is not None:
            on_result(result)
    return results


def format_summary(results: Sequence[SeriesResult]) -> str:
    """Plain-text summary table (no values)."""
    header = ("series", "rows", "date_min", "date_max", "status", "detail")
    rows = [
        (
            r.series_id,
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
        prog="python -m mrm.ingest.macro",
        description="Load FRED macro series into DuckDB (90-day overlap or full reload).",
    )
    parser.add_argument("--series", help="Comma-separated subset of config/macro.yaml")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Fetch and validate (calls the API), but write nothing to the warehouse",
    )
    return parser.parse_args(argv)


def main(
    argv: Sequence[str] | None = None,
    *,
    client: MacroClient | None = None,
    con: duckdb.DuckDBPyConnection | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> int:
    """CLI entry point. ``client``/``con``/``sleep`` are injectable for tests.

    Exit codes: 0 all series ok or no_data, 1 some series failed, 2 usage/config error.
    """
    args = _parse_args(argv)
    configured = load_macro_series()
    if args.series:
        by_id = {s.series_id: s for s in configured}
        wanted = [s.strip().upper() for s in args.series.split(",") if s.strip()]
        blocked = [s for s in wanted if s in BLOCKED_SERIES]
        if blocked:
            print(f"error: copyrighted series are not ingested: {', '.join(blocked)}")
            return 2
        unknown = [s for s in wanted if s not in by_id]
        if unknown or not wanted:
            print(f"error: series not in config/macro.yaml: {', '.join(unknown) or '(none)'}")
            return 2
        selected = [by_id[s] for s in wanted]
    else:
        selected = configured

    secret: str | None = None
    if client is None:
        load_dotenv()
        secret = os.environ.get(API_KEY_ENV, "").strip() or None
        if secret is None:
            print(f"error: {API_KEY_ENV} is not set (see .env.example)")
            return 2
        client = FredClient(secret, sleep=sleep)

    own_con = False
    if con is None and not args.dry_run:
        con = connect()
        own_con = True
    try:
        if con is not None and not args.dry_run:
            create_schema(con)
        mode = " (dry run: nothing is written)" if args.dry_run else ""
        print(f"Ingesting {len(selected)} series from {client.name}{mode}")

        def report(result: SeriesResult) -> None:
            print(f"  {result.series_id}: {result.status} ({result.rows} rows)", flush=True)

        results = ingest_macro(
            con,
            client,
            selected,
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
