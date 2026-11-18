"""Data quality checks over ``prices_clean`` (``raw_prices`` + documented adjustments) and
``raw_macro``.

Each check is ``(con, cfg, today) -> list[Finding]`` and only reads the warehouse.
Findings are aggregated per (check, entity, severity): ``n_affected`` counts the rows/dates
involved and ``detail`` lists them (truncated). Nothing is imputed or modified.
"""

from collections import Counter, defaultdict
from collections.abc import Callable, Iterable, Sequence
from datetime import date, timedelta

import duckdb

from mrm.config import DataQualityConfig
from mrm.quality.calendar import (
    is_nyse_business_day,
    last_nyse_business_day,
    market_closed_days,
    nyse_business_days,
)
from mrm.storage.models import Finding

CRITICAL, WARNING, INFO = "critical", "warning", "info"
MAX_LISTED = 10  # dates/runs listed in a finding's detail before truncating

Check = Callable[[duckdb.DuckDBPyConnection, DataQualityConfig, date], list[Finding]]


def _listing(items: Sequence[str], limit: int = MAX_LISTED) -> str:
    shown = ", ".join(items[:limit])
    return shown if len(items) <= limit else f"{shown}, ... (+{len(items) - limit} more)"


def _series_by_key(
    con: duckdb.DuckDBPyConnection, sql: str
) -> dict[str, list[tuple[date, float | None]]]:
    grouped: dict[str, list[tuple[date, float | None]]] = defaultdict(list)
    for key, day, value in con.execute(sql).fetchall():
        grouped[key].append((day, value))
    return grouped


# --- prices --------------------------------------------------------------------------------


def check_price_duplicates(
    con: duckdb.DuckDBPyConnection,
    cfg: DataQualityConfig,
    today: date,
    table: str = "prices_clean",
) -> list[Finding]:
    """Critical: more than one row per (ticker, trade_date). The PK prevents it today; this
    guards against schema changes. ``table`` is a fixed identifier, never user input."""
    rows = con.execute(
        f"SELECT ticker, trade_date, count(*) FROM {table} "
        "GROUP BY ticker, trade_date HAVING count(*) > 1 ORDER BY ticker, trade_date"
    ).fetchall()
    by_ticker: dict[str, list[str]] = defaultdict(list)
    for ticker, day, n in rows:
        by_ticker[ticker].append(f"{day} (x{n})")
    return [
        Finding("price_duplicates", t, CRITICAL, len(days), _listing(days))
        for t, days in sorted(by_ticker.items())
    ]


def check_price_non_positive(
    con: duckdb.DuckDBPyConnection, cfg: DataQualityConfig, today: date
) -> list[Finding]:
    """Critical: any open/high/low/close <= 0."""
    rows = con.execute(
        "SELECT ticker, trade_date FROM prices_clean "
        "WHERE close <= 0 OR open <= 0 OR high <= 0 OR low <= 0 ORDER BY ticker, trade_date"
    ).fetchall()
    by_ticker: dict[str, list[str]] = defaultdict(list)
    for ticker, day in rows:
        by_ticker[ticker].append(str(day))
    return [
        Finding("price_non_positive", t, CRITICAL, len(days), _listing(days))
        for t, days in sorted(by_ticker.items())
    ]


def _missing_runs(expected: list[date], present: set[date]) -> list[list[date]]:
    """Runs of consecutive expected trading days absent from ``present``."""
    runs: list[list[date]] = []
    current: list[date] = []
    for day in expected:
        if day in present:
            if current:
                runs.append(current)
                current = []
        else:
            current.append(day)
    if current:
        runs.append(current)
    return runs


def _run_label(run: list[date]) -> str:
    return str(run[0]) if len(run) == 1 else f"{run[0]}..{run[-1]} ({len(run)}d)"


def check_price_missing_dates(
    con: duckdb.DuckDBPyConnection, cfg: DataQualityConfig, today: date
) -> list[Finding]:
    """Missing NYSE trading days between each ticker's own first and last date.

    A run longer than ``max_missing_gap_days`` is critical, shorter runs are warnings.
    Dates that are not trading days (weekend, holiday, special closure) are warnings too.
    """
    closures = cfg.special_closures
    dates: dict[str, set[date]] = defaultdict(set)
    for ticker, day in con.execute("SELECT ticker, trade_date FROM prices_clean").fetchall():
        dates[ticker].add(day)

    findings: list[Finding] = []
    for ticker, present in sorted(dates.items()):
        expected = nyse_business_days(min(present), max(present), closures)
        runs = _missing_runs(expected, present)
        for severity, selected in (
            (CRITICAL, [r for r in runs if len(r) > cfg.max_missing_gap_days]),
            (WARNING, [r for r in runs if len(r) <= cfg.max_missing_gap_days]),
        ):
            if selected:
                n = sum(len(r) for r in selected)
                detail = f"{len(selected)} gap(s): " + _listing([_run_label(r) for r in selected])
                findings.append(Finding("price_missing_dates", ticker, severity, n, detail))
        extra = sorted(d for d in present if not is_nyse_business_day(d, closures))
        if extra:
            findings.append(
                Finding(
                    "price_non_trading_dates",
                    ticker,
                    WARNING,
                    len(extra),
                    _listing([str(d) for d in extra]),
                )
            )
    return findings


def _top_moves(moves: list[tuple[date, float]], n: int) -> str:
    top = sorted(moves, key=lambda m: abs(m[1]), reverse=True)[:n]
    return ", ".join(f"{d} {r:+.2%}" for d, r in top)


def check_price_extreme_returns(
    con: duckdb.DuckDBPyConnection, cfg: DataQualityConfig, today: date
) -> list[Finding]:
    """|simple daily return| above ``extreme_return_abs``: warning, or info inside a known
    stress window. Detail: the count plus the ``extreme_return_top_n`` largest moves."""
    prices = _series_by_key(
        con, "SELECT ticker, trade_date, close FROM prices_clean ORDER BY ticker, trade_date"
    )
    findings: list[Finding] = []
    for ticker, rows in sorted(prices.items()):
        flagged: list[tuple[date, float]] = []
        known: list[tuple[date, float]] = []
        reasons: set[str] = set()
        for (_, prev), (day, close) in zip(rows, rows[1:]):
            ret = close / prev - 1
            if abs(ret) <= cfg.extreme_return_abs:
                continue
            window = next((w for w in cfg.known_extreme_returns if w.covers(ticker, day)), None)
            if window is None:
                flagged.append((day, ret))
            else:
                known.append((day, ret))
                reasons.add(window.reason)
        top_n = cfg.extreme_return_top_n
        threshold = f"|r| > {cfg.extreme_return_abs:.0%}"
        if flagged:
            detail = f"{len(flagged)} day(s) {threshold}; top: {_top_moves(flagged, top_n)}"
            findings.append(Finding("price_extreme_returns", ticker, WARNING, len(flagged), detail))
        if known:
            detail = (
                f"{len(known)} day(s) {threshold} in known windows "
                f"({', '.join(sorted(reasons))}); top: {_top_moves(known, top_n)}"
            )
            findings.append(Finding("price_extreme_returns", ticker, INFO, len(known), detail))
    return findings


def check_price_staleness(
    con: duckdb.DuckDBPyConnection, cfg: DataQualityConfig, today: date
) -> list[Finding]:
    """Critical: a universe ticker (``dim_asset``) whose last bar is more than
    ``stale_after_business_days`` NYSE trading days behind the last trading day, or has no bars."""
    closures = cfg.special_closures
    reference = last_nyse_business_day(today, closures)
    rows = con.execute(
        "SELECT a.ticker, max(p.trade_date) FROM dim_asset a "
        "LEFT JOIN prices_clean p USING (ticker) GROUP BY a.ticker ORDER BY a.ticker"
    ).fetchall()
    findings: list[Finding] = []
    for ticker, last in rows:
        if last is None:
            findings.append(Finding("price_staleness", ticker, CRITICAL, 0, "no price data"))
            continue
        if last >= reference:
            continue
        lag = len(nyse_business_days(last + timedelta(days=1), reference, closures))
        if lag > cfg.stale_after_business_days:
            detail = (
                f"last bar {last}, {lag} trading day(s) behind {reference} "
                f"(limit {cfg.stale_after_business_days})"
            )
            findings.append(Finding("price_staleness", ticker, CRITICAL, lag, detail))
    return findings


def check_price_adjustments(
    con: duckdb.DuckDBPyConnection, cfg: DataQualityConfig, today: date
) -> list[Finding]:
    """Status of each documented price adjustment (``price_adjustment_status``).

    Info: applied (raw ex-date return above its threshold). Warning: not applied, because
    the raw return is at or below the threshold (the provider may have fixed its data) or
    because the ex-date or the trading day before it is missing in ``raw_prices``.
    """
    rows = con.execute(
        "SELECT ticker, adjustment_id, ex_date, multiplier, apply_if_raw_return_above, "
        "prev_close, ex_close, raw_return, applied "
        "FROM price_adjustment_status ORDER BY ticker, ex_date"
    ).fetchall()
    grouped: dict[tuple[str, str], list[str]] = defaultdict(list)
    for ticker, aid, ex_date, mult, threshold, prev_close, ex_close, raw_return, applied in rows:
        if applied:
            note = (
                f"applied ×{mult:g} before {ex_date}; "
                f"raw return {raw_return:+.2%} > {threshold:.0%} ({aid})"
            )
            grouped[(ticker, INFO)].append(note)
        elif ex_close is None:
            note = f"ex_date {ex_date} missing in raw_prices; adjustment skipped ({aid})"
            grouped[(ticker, WARNING)].append(note)
        elif prev_close is None:
            note = f"no trading day before ex_date {ex_date}; adjustment skipped ({aid})"
            grouped[(ticker, WARNING)].append(note)
        else:
            note = (
                f"raw return on {ex_date} {raw_return:+.2%} <= {threshold:.0%}: provider may "
                f"have fixed the data; adjustment skipped ({aid})"
            )
            grouped[(ticker, WARNING)].append(note)
    return [
        Finding("price_adjustments", ticker, severity, len(notes), "; ".join(notes))
        for (ticker, severity), notes in sorted(grouped.items())
    ]


# --- macro ---------------------------------------------------------------------------------


def check_macro_freshness(
    con: duckdb.DuckDBPyConnection, cfg: DataQualityConfig, today: date
) -> list[Finding]:
    """Critical: the last non-missing observation is older than ``max_age_days`` of the
    series' frequency, or a configured series has no data. Warning: stored series without a
    configured frequency."""
    last_by_series = dict(
        con.execute(
            "SELECT series_id, max(obs_date) FILTER (WHERE value IS NOT NULL) "
            "FROM raw_macro GROUP BY series_id"
        ).fetchall()
    )
    findings: list[Finding] = []
    for series_id in sorted(set(cfg.series_frequency) | set(last_by_series)):
        frequency = cfg.series_frequency.get(series_id)
        last = last_by_series.get(series_id)
        if frequency is None:
            findings.append(
                Finding("macro_freshness", series_id, WARNING, 0, "frequency not configured")
            )
        elif last is None:
            findings.append(Finding("macro_freshness", series_id, CRITICAL, 0, "no data"))
        elif (age := (today - last).days) > cfg.max_age_days[frequency]:
            limit = cfg.max_age_days[frequency]
            detail = f"{frequency}: last value {last}, {age} days old (limit {limit})"
            findings.append(Finding("macro_freshness", series_id, CRITICAL, age, detail))
    return findings


def _count_label(counter: Counter[str]) -> str:
    return ", ".join(f"{reason} {n}" for reason, n in sorted(counter.items()))


def check_macro_gaps(
    con: duckdb.DuckDBPyConnection, cfg: DataQualityConfig, today: date
) -> list[Finding]:
    """Classify every FRED missing value ('.', stored as NULL).

    Info: known gap (``known_gaps``); for daily series also a market/bond holiday (federal,
    NYSE incl. Good Friday, special closure) or a date before ``classify_gaps_from``
    (reported in aggregate). Warning: anything else (unexplained). Nothing is imputed.
    """
    nulls = _series_by_key(
        con,
        "SELECT series_id, obs_date, value FROM raw_macro WHERE value IS NULL "
        "ORDER BY series_id, obs_date",
    )
    findings: list[Finding] = []
    for series_id, rows in sorted(nulls.items()):
        daily = cfg.series_frequency.get(series_id) == "daily"
        days = [d for d, _ in rows]
        reasons = market_closed_days(days, cfg.special_closures.keys()) if daily else {}
        explained: Counter[str] = Counter()
        known: list[str] = []
        unexplained: list[str] = []
        for day in days:
            if (series_id, day) in cfg.known_gaps:
                known.append(f"{day} ({cfg.known_gaps[(series_id, day)]})")
            elif daily and day < cfg.classify_gaps_from:
                explained[f"before {cfg.classify_gaps_from} (not classified)"] += 1
            elif day in reasons:
                explained[reasons[day]] += 1
            else:
                unexplained.append(str(day))
        if unexplained:
            findings.append(
                Finding(
                    "macro_gaps",
                    series_id,
                    WARNING,
                    len(unexplained),
                    "unexplained: " + _listing(unexplained),
                )
            )
        if known or explained:
            parts = [_count_label(explained)] if explained else []
            if known:
                parts.append("known gap: " + _listing(known))
            n = len(known) + sum(explained.values())
            findings.append(Finding("macro_gaps", series_id, INFO, n, "; ".join(parts)))
    return findings


# --- both ----------------------------------------------------------------------------------


def check_future_dates(
    con: duckdb.DuckDBPyConnection, cfg: DataQualityConfig, today: date
) -> list[Finding]:
    """Critical: any price or macro row dated after ``today``."""
    rows = con.execute(
        "SELECT 'prices_clean', ticker, trade_date FROM prices_clean WHERE trade_date > $1 "
        "UNION ALL "
        "SELECT 'raw_macro', series_id, obs_date FROM raw_macro WHERE obs_date > $1 "
        "ORDER BY 1, 2, 3",
        [today],
    ).fetchall()
    by_entity: dict[tuple[str, str], list[str]] = defaultdict(list)
    for table, entity, day in rows:
        by_entity[(table, entity)].append(str(day))
    return [
        Finding("future_dates", f"{table}:{entity}", CRITICAL, len(days), _listing(days))
        for (table, entity), days in sorted(by_entity.items())
    ]


ALL_CHECKS: tuple[Check, ...] = (
    check_price_duplicates,
    check_price_non_positive,
    check_price_missing_dates,
    check_price_extreme_returns,
    check_price_staleness,
    check_price_adjustments,
    check_macro_freshness,
    check_macro_gaps,
    check_future_dates,
)


def run_checks(
    con: duckdb.DuckDBPyConnection,
    cfg: DataQualityConfig,
    today: date,
    checks: Iterable[Check] = ALL_CHECKS,
) -> list[Finding]:
    """Run every check and return all findings."""
    return [finding for check in checks for finding in check(con, cfg, today)]
