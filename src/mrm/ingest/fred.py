"""FRED ``series/observations`` client; see docs/decisions/0002-macro-series-licensing.md.

Security: the API key travels as a query parameter, so request URLs, ``params`` and the
text of ``requests`` exceptions are never logged or chained into raised errors. Errors
carry only the exception type and the HTTP status / FRED error code (never FRED's text).
"""

import json
import math
import os
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

import requests

from mrm.config import REPO_ROOT
from mrm.ingest.providers import ProviderError, RateLimitError
from mrm.storage.models import MacroObservation

BASE_URL = "https://api.stlouisfed.org/fred/series/observations"
SOURCE_NAME = "fred"
MISSING_VALUE = "."  # FRED's marker for a missing observation
DEFAULT_TIMEOUT = 30.0
DEFAULT_RETRY_WAIT = 20.0
DEFAULT_RAW_DIR: Path = REPO_ROOT / "data" / "raw" / "fred"


@dataclass(frozen=True, slots=True)
class MacroFetchResult:
    """Validated observations of one series, ascending by date (``value`` None = missing)."""

    series_id: str
    observations: list[MacroObservation] = field(default_factory=list)
    missing: int = 0


def _parse_value(raw: Any, obs_date: date) -> float | None:
    if raw == MISSING_VALUE:
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        value = math.nan
    if not math.isfinite(value):
        raise ProviderError(f"invalid value on {obs_date.isoformat()}")
    return value


def parse_observations(payload: dict[str, Any], series_id: str) -> MacroFetchResult:
    """Validate an observations payload and convert it to ``MacroObservation`` rows.

    ``"."`` becomes a NULL value (counted in ``missing``). Any other non-numeric value,
    an unparsable date, dates that are not strictly ascending, or fewer rows than the
    reported ``count`` (truncated response) fail the whole series.
    """
    rows = payload.get("observations")
    if rows is None:
        rows = []
    if not isinstance(rows, list):
        raise ProviderError("invalid payload: observations is not a list")
    count = payload.get("count")
    if isinstance(count, int) and count > len(rows):
        raise ProviderError("truncated response")

    observations: list[MacroObservation] = []
    missing = 0
    previous: date | None = None
    for row in rows:
        if not isinstance(row, dict):
            raise ProviderError("invalid payload: observation is not an object")
        try:
            obs_date = date.fromisoformat(str(row.get("date", "")))
        except ValueError:
            raise ProviderError("invalid payload: unparsable date") from None
        if previous is not None and obs_date <= previous:
            raise ProviderError(f"dates not strictly ascending at {obs_date.isoformat()}")
        previous = obs_date
        value = _parse_value(row.get("value"), obs_date)
        missing += value is None
        observations.append(MacroObservation(series_id, obs_date, value))
    return MacroFetchResult(series_id, observations, missing)


class FredClient:
    """Observations of FRED series, one request per series (latest vintage)."""

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
    ) -> None:
        if not api_key or not api_key.strip():
            raise ValueError("FRED API key is empty")
        self._api_key = api_key.strip()
        self._session = session if session is not None else requests.Session()
        self._timeout = timeout
        self._retry_wait = retry_wait
        self._sleep = sleep
        self._raw_dir = raw_dir

    def __repr__(self) -> str:
        return f"FredClient(timeout={self._timeout}, raw_dir={self._raw_dir})"

    def fetch_observations(self, series_id: str, start: date | None) -> MacroFetchResult:
        """Fetch, cache and validate ``series_id`` from ``start`` (None = full history).

        On a rate-limit error it waits ``retry_wait`` seconds and retries once.
        """
        series_id = series_id.strip().upper()
        try:
            payload = self._request(series_id, start)
        except RateLimitError:
            self._sleep(self._retry_wait)
            payload = self._request(series_id, start)
        self._save_raw(series_id, payload)
        return parse_observations(payload, series_id)

    def _request(self, series_id: str, start: date | None) -> dict[str, Any]:
        """One GET attempt. Errors are built outside ``except`` blocks so the original
        ``requests`` exception (whose text holds the URL and key) is never chained."""
        params: dict[str, Any] = {
            "series_id": series_id,
            "file_type": "json",
            "sort_order": "asc",
            "api_key": self._api_key,
        }
        if start is not None:
            params["observation_start"] = start.isoformat()

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
        payload: Any = None
        try:
            payload = response.json()
        except ValueError:
            payload = None
        code = payload.get("error_code") if isinstance(payload, dict) else None

        if status_code == 429 or str(code) == "429":
            raise RateLimitError("HTTP 429")
        if code is not None:
            raise ProviderError(f"api_error code={code}")  # FRED's message is not copied
        if status_code != 200:
            raise ProviderError(f"HTTP {status_code}")
        if payload is None:
            raise ProviderError("invalid JSON")
        if not isinstance(payload, dict):
            raise ProviderError("invalid JSON: not an object")
        return payload

    def _save_raw(self, series_id: str, payload: dict[str, Any]) -> None:
        """Write the raw response to ``<raw_dir>/<ID>.json`` atomically (gitignored)."""
        if self._raw_dir is None:
            return
        self._raw_dir.mkdir(parents=True, exist_ok=True)
        target = self._raw_dir / f"{series_id}.json"
        tmp = target.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload), encoding="utf-8")
        os.replace(tmp, target)
