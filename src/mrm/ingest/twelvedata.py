"""Twelve Data ``/time_series`` client (daily, ``adjust=all``); see ADR 0001.

Security: the API key travels as a query parameter, so request URLs, ``params`` and the
text of ``requests`` exceptions are never logged or chained into raised errors. Errors
carry only the exception type and the HTTP status / API error code.
"""

import json
import math
import os
import time
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from datetime import time as dtime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import requests

from mrm.config import REPO_ROOT
from mrm.ingest.providers import FetchResult, ProviderError, RateLimitError, redact
from mrm.storage.models import PriceBar

BASE_URL = "https://api.twelvedata.com/time_series"
SOURCE_NAME = "twelvedata"
DEFAULT_TIMEOUT = 30.0
DEFAULT_RETRY_WAIT = 60.0
OUTPUTSIZE = 5000
MAX_STALENESS_DAYS = 10
NEW_YORK = ZoneInfo("America/New_York")
# Before this New York time, a bar dated today may still be intraday (close 16:00 + margin).
PARTIAL_BAR_CUTOFF = dtime(16, 30)
DEFAULT_RAW_DIR: Path = REPO_ROOT / "data" / "raw" / "twelvedata"
# Names Twelve Data may use for US listing venues (not all verified against the live API).
US_EXCHANGES: frozenset[str] = frozenset(
    {
        "NYSE",
        "NASDAQ",
        "NYSE ARCA",
        "NYSEARCA",
        "ARCA",
        "BATS",
        "CBOE",
        "CBOE BZX",
        "AMEX",
        "NYSE AMERICAN",
    }  # fmt: skip
)


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _to_float(value: Any) -> float | None:
    """Parse a numeric field; None for missing, empty, non-numeric or non-finite values."""
    if value is None or value == "":
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _to_int(value: Any) -> int | None:
    as_float = _to_float(value)
    return None if as_float is None else int(as_float)


def validate_meta(meta: Any, ticker: str) -> None:
    """Require the response to describe ``ticker``, priced in USD, on a US exchange."""
    if not isinstance(meta, dict):
        raise ProviderError("invalid meta: missing")
    symbol = str(meta.get("symbol") or "").strip().upper()
    if symbol != ticker:
        raise ProviderError(f"invalid meta: symbol={symbol or 'missing'}")
    currency = str(meta.get("currency") or "").strip().upper()
    if currency != "USD":
        raise ProviderError(f"invalid meta: currency={currency or 'missing'}")
    exchange = str(meta.get("exchange") or "").strip().upper()
    if exchange not in US_EXCHANGES:
        raise ProviderError(f"invalid meta: exchange={exchange or 'missing'}")


def parse_time_series(payload: dict[str, Any], ticker: str) -> FetchResult:
    """Validate a ``/time_series`` payload and convert it to ascending ``PriceBar`` rows.

    Rows with a null close are dropped (and counted). A non-numeric or non-positive close,
    an unparsable date, or dates that are not strictly ascending fail the whole ticker.
    """
    validate_meta(payload.get("meta"), ticker)
    values = payload.get("values") or []
    if not isinstance(values, list):
        raise ProviderError("invalid payload: values is not a list")

    bars: list[PriceBar] = []
    dropped = 0
    previous: date | None = None
    for row in values:
        if not isinstance(row, dict):
            raise ProviderError("invalid payload: row is not an object")
        try:
            trade_date = date.fromisoformat(str(row.get("datetime", ""))[:10])
        except ValueError:
            raise ProviderError("invalid payload: unparsable datetime") from None
        if previous is not None and trade_date <= previous:
            raise ProviderError(f"dates not strictly ascending at {trade_date.isoformat()}")
        previous = trade_date

        raw_close = row.get("close")
        if raw_close is None or raw_close == "":
            dropped += 1
            continue
        close = _to_float(raw_close)
        if close is None or close <= 0:
            raise ProviderError(f"invalid close on {trade_date.isoformat()}")
        bars.append(
            PriceBar(
                ticker=ticker,
                trade_date=trade_date,
                open=_to_float(row.get("open")),
                high=_to_float(row.get("high")),
                low=_to_float(row.get("low")),
                close=close,
                volume=_to_int(row.get("volume")),
            )
        )
    return FetchResult(ticker=ticker, bars=bars, dropped_null_close=dropped)


def drop_partial_bar(result: FetchResult, now: datetime) -> FetchResult:
    """Drop the last bar if it is dated today (New York) and NY time is before 16:30.

    During the session the provider returns the current day as a still-moving bar; storing
    it as a close would be wrong. Bars of previous days are never dropped. ``now`` must be
    timezone-aware.
    """
    if not result.bars:
        return result
    now_ny = now.astimezone(NEW_YORK)
    last = result.bars[-1].trade_date
    if last == now_ny.date() and now_ny.time() < PARTIAL_BAR_CUTOFF:
        return replace(result, bars=result.bars[:-1], dropped_partial_bar=True)
    return result


def check_freshness(result: FetchResult, today: date, max_age_days: int) -> None:
    """Fail if the newest bar is older than ``max_age_days`` calendar days.

    Guards against a response holding the oldest bars of a long history instead of the
    most recent ones.
    """
    if not result.bars:
        return
    last = result.bars[-1].trade_date
    if last < today - timedelta(days=max_age_days):
        raise ProviderError(f"stale data: last date {last.isoformat()}")


def _is_rate_limit(payload: dict[str, Any]) -> bool:
    if str(payload.get("code")) == "429":
        return True
    message = str(payload.get("message", "")).lower()
    return "credits" in message or "rate limit" in message


class TwelveDataProvider:
    """Daily adjusted bars from Twelve Data, one request per ticker (full history)."""

    name = SOURCE_NAME

    def __init__(
        self,
        api_key: str,
        *,
        session: Any | None = None,
        timeout: float = DEFAULT_TIMEOUT,
        retry_wait: float = DEFAULT_RETRY_WAIT,
        sleep: Callable[[float], None] = time.sleep,
        raw_dir: Path | None = DEFAULT_RAW_DIR,
        now: Callable[[], datetime] = _utc_now,
        max_staleness_days: int = MAX_STALENESS_DAYS,
    ) -> None:
        if not api_key or not api_key.strip():
            raise ValueError("Twelve Data API key is empty")
        self._api_key = api_key.strip()
        self._session = session if session is not None else requests.Session()
        self._timeout = timeout
        self._retry_wait = retry_wait
        self._sleep = sleep
        self._raw_dir = raw_dir
        self._now = now
        self._max_staleness_days = max_staleness_days

    def __repr__(self) -> str:
        return f"TwelveDataProvider(timeout={self._timeout}, raw_dir={self._raw_dir})"

    def fetch_daily(self, ticker: str) -> FetchResult:
        """Fetch, cache and validate the full daily history of ``ticker``.

        On a rate-limit error it waits ``retry_wait`` seconds and retries once. A bar for
        today is dropped before 16:30 New York time (``now`` is injectable for tests).
        """
        ticker = ticker.strip().upper()
        try:
            payload = self._request(ticker)
        except RateLimitError:
            self._sleep(self._retry_wait)
            payload = self._request(ticker)
        self._save_raw(ticker, payload)
        now = self._now()
        result = drop_partial_bar(parse_time_series(payload, ticker), now)
        check_freshness(result, now.astimezone(UTC).date(), self._max_staleness_days)
        return result

    def _request(self, ticker: str) -> dict[str, Any]:
        """One GET attempt. Errors are built outside ``except`` blocks so the original
        ``requests`` exception (whose text holds the URL and key) is never chained."""
        params = {
            "symbol": ticker,
            "interval": "1day",
            "adjust": "all",
            "outputsize": OUTPUTSIZE,
            "order": "asc",
            "apikey": self._api_key,
        }
        error: ProviderError | None = None
        response = None
        try:
            response = self._session.get(BASE_URL, params=params, timeout=self._timeout)
        except requests.RequestException as exc:
            status = getattr(getattr(exc, "response", None), "status_code", None)
            error = ProviderError(f"{type(exc).__name__} status={status or 'N/A'}")
        if error is not None:
            raise error

        status_code = getattr(response, "status_code", None)
        if status_code == 429:
            raise RateLimitError("HTTP 429")
        if status_code != 200:
            raise ProviderError(f"HTTP {status_code}")

        payload: Any = None
        try:
            payload = response.json()
        except ValueError:
            error = ProviderError("invalid JSON")
        if error is not None:
            raise error
        if not isinstance(payload, dict):
            raise ProviderError("invalid JSON: not an object")

        if str(payload.get("status", "")).lower() == "error":
            code = payload.get("code", "N/A")
            if _is_rate_limit(payload):
                raise RateLimitError(f"api_error code={code}")
            raise ProviderError(redact(f"api_error code={code}", self._api_key))
        return payload

    def _save_raw(self, ticker: str, payload: dict[str, Any]) -> None:
        """Write the raw response to ``<raw_dir>/<TICKER>.json`` atomically (gitignored)."""
        if self._raw_dir is None:
            return
        self._raw_dir.mkdir(parents=True, exist_ok=True)
        target = self._raw_dir / f"{ticker}.json"
        tmp = target.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload), encoding="utf-8")
        os.replace(tmp, target)
