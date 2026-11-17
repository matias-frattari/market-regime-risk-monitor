# SYNTHETIC — not real market data. Every payload below is a made-up Twelve Data-shaped
# response with obviously fake round prices, used only to exercise the ingestion code.
# No test touches the network: HTTP is served by FakeSession.
from collections.abc import Callable, Iterator
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import duckdb
import pytest
import requests

from mrm.ingest import prices
from mrm.ingest.prices import (
    ABORTED_MESSAGE,
    PACE_SECONDS,
    format_summary,
    ingest_prices,
    ingest_ticker,
    main,
)
from mrm.ingest.providers import FetchResult, ProviderError, redact
from mrm.ingest.twelvedata import (
    BASE_URL,
    DEFAULT_RETRY_WAIT,
    TwelveDataProvider,
    parse_time_series,
)
from mrm.storage.loaders import replace_prices
from mrm.storage.models import PriceBar

SECRET = "SYNTHETIC-SECRET-KEY"
TODAY = date(2030, 1, 15)  # fixed "today" for the freshness check
NY = ZoneInfo("America/New_York")
# Default clock: 17:00 New York on TODAY, i.e. after the close, so no bar is dropped.
AFTER_CLOSE = datetime(2030, 1, 15, 17, 0, tzinfo=NY)


# --- fakes -------------------------------------------------------------------------------


class FakeResponse:
    def __init__(self, status_code: int = 200, body: Any = None, bad_json: bool = False):
        self.status_code = status_code
        self._body = body
        self._bad_json = bad_json

    def json(self) -> Any:
        if self._bad_json:
            raise ValueError("Expecting value")
        return self._body


Reply = FakeResponse | Exception


class FakeSession:
    """Serves queued replies per symbol and records every call."""

    def __init__(self, replies: dict[str, list[Reply]]):
        self.replies = {k: list(v) for k, v in replies.items()}
        self.calls: list[dict[str, Any]] = []

    def get(self, url: str, params: dict[str, Any], timeout: float) -> FakeResponse:
        self.calls.append({"url": url, "params": dict(params), "timeout": timeout})
        reply = self.replies[params["symbol"]].pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply


class SleepRecorder:
    def __init__(self) -> None:
        self.waits: list[float] = []

    def __call__(self, seconds: float) -> None:
        self.waits.append(seconds)


def meta(ticker: str, **overrides: Any) -> dict[str, Any]:
    base = {"symbol": ticker, "currency": "USD", "exchange": "NYSE", "interval": "1day"}
    base.update(overrides)
    return {k: v for k, v in base.items() if v is not None}


def rows(n: int, end: date = TODAY, close: str = "100.0") -> list[dict[str, Any]]:
    """SYNTHETIC — n consecutive days ending at ``end``, ascending, flat fake price."""
    start = end - timedelta(days=n - 1)
    return [
        {
            "datetime": (start + timedelta(days=i)).isoformat(),
            "open": close,
            "high": close,
            "low": close,
            "close": close,
            "volume": "1000",
        }
        for i in range(n)
    ]


def ok(ticker: str, values: list[dict[str, Any]] | None = None, **meta_overrides: Any) -> Reply:
    body = {"meta": meta(ticker, **meta_overrides), "values": values, "status": "ok"}
    return FakeResponse(200, body if values is not None else {**body, "values": rows(3)})


def api_error(code: int, message: str = "SYNTHETIC error") -> Reply:
    return FakeResponse(200, {"code": code, "message": message, "status": "error"})


def make_provider(
    session: FakeSession,
    sleep: SleepRecorder,
    raw_dir: Path | None,
    now: datetime = AFTER_CLOSE,
) -> TwelveDataProvider:
    return TwelveDataProvider(
        SECRET, session=session, sleep=sleep, raw_dir=raw_dir, now=lambda: now
    )


@pytest.fixture
def sleep() -> SleepRecorder:
    return SleepRecorder()


@pytest.fixture
def run(
    con: duckdb.DuckDBPyConnection, sleep: SleepRecorder, tmp_path: Path
) -> Callable[..., tuple[list[prices.TickerResult], FakeSession]]:
    """Run ingest_prices against a FakeSession serving ``replies``."""

    def _run(
        replies: dict[str, list[Reply]], dry_run: bool = False
    ) -> tuple[list[prices.TickerResult], FakeSession]:
        session = FakeSession(replies)
        provider = make_provider(session, sleep, tmp_path / "raw")
        results = ingest_prices(
            con, provider, list(replies), sleep=sleep, dry_run=dry_run, secret=SECRET
        )
        return results, session

    return _run


def price_count(con: duckdb.DuckDBPyConnection, ticker: str) -> int:
    return con.execute("SELECT count(*) FROM raw_prices WHERE ticker = ?", [ticker]).fetchone()[0]


def log_rows(con: duckdb.DuckDBPyConnection) -> list[tuple]:
    return con.execute(
        "SELECT entity, status, rows_loaded, date_from, date_to, message "
        "FROM ingest_log ORDER BY log_id"
    ).fetchall()


def seed(con: duckdb.DuckDBPyConnection, ticker: str, n: int = 5) -> None:
    """SYNTHETIC — pre-existing history that a failed load must not delete."""
    start = date(2020, 1, 1)
    bars = [
        PriceBar(ticker, start + timedelta(days=i), 50.0, 50.0, 50.0, 50.0, 10) for i in range(n)
    ]
    replace_prices(con, ticker, bars)


@pytest.fixture(autouse=True)
def no_dotenv(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Never load the developer's real .env during tests."""
    monkeypatch.setattr(prices, "load_dotenv", lambda *a, **k: False)
    yield


# --- happy path --------------------------------------------------------------------------


def test_successful_load(run, con, tmp_path: Path) -> None:
    results, session = run({"SPY": [ok("SPY", rows(4))]})
    (r,) = results
    assert (r.status, r.rows) == ("success", 4)
    assert (r.date_min, r.date_max) == (TODAY - timedelta(days=3), TODAY)
    assert price_count(con, "SPY") == 4
    assert log_rows(con) == [("SPY", "success", 4, r.date_min, TODAY, None)]
    call = session.calls[0]
    assert call["url"] == BASE_URL and call["timeout"] > 0
    p = call["params"]
    assert (p["interval"], p["adjust"], p["outputsize"], p["order"]) == (
        "1day",
        "all",
        5000,
        "asc",
    )
    assert (tmp_path / "raw" / "SPY.json").exists()


def test_reload_replaces_existing_history(run, con) -> None:
    seed(con, "SPY", n=10)
    run({"SPY": [ok("SPY", rows(3))]})
    assert price_count(con, "SPY") == 3


def test_volume_parsed_to_int_and_optional_fields() -> None:
    values = [{"datetime": "2030-01-14", "close": "10.5", "volume": "1234.0", "open": ""}]
    result = parse_time_series({"meta": meta("SPY"), "values": values}, "SPY")
    (bar,) = result.bars
    assert bar.volume == 1234 and isinstance(bar.volume, int)
    assert bar.open is None and bar.high is None and bar.close == 10.5


# --- API / HTTP errors -------------------------------------------------------------------


def test_json_error_with_http_200_keeps_existing_data(run, con) -> None:
    seed(con, "SPY")
    (r,), _ = run({"SPY": [api_error(400, "SYNTHETIC symbol not found")]})
    assert r.status == "error" and r.message == "api_error code=400"
    assert price_count(con, "SPY") == 5
    assert log_rows(con)[0][1:3] == ("error", 0)


@pytest.mark.parametrize("first", [FakeResponse(429, {}), api_error(429, "run out of credits")])
def test_rate_limit_waits_and_retries_once(run, con, sleep, first) -> None:
    (r,), session = run({"SPY": [first, ok("SPY", rows(2))]})
    assert r.status == "success" and r.rows == 2
    assert sleep.waits == [DEFAULT_RETRY_WAIT]
    assert len(session.calls) == 2


def test_second_rate_limit_is_error_and_next_ticker_continues(run, con, sleep) -> None:
    results, _ = run(
        {
            "SPY": [FakeResponse(429, {}), FakeResponse(429, {})],
            "TLT": [ok("TLT", rows(2))],
        }
    )
    assert [r.status for r in results] == ["error", "success"]
    assert results[0].rate_limited and results[0].message == "HTTP 429"
    assert sleep.waits == [DEFAULT_RETRY_WAIT, PACE_SECONDS]


def test_two_consecutive_rate_limits_abort_the_run(run, con, sleep) -> None:
    limited = [FakeResponse(429, {}), FakeResponse(429, {})]
    results, session = run({"SPY": list(limited), "QQQ": list(limited), "TLT": [], "GLD": []})
    assert [r.status for r in results] == ["error"] * 4
    assert [r.message for r in results[2:]] == [ABORTED_MESSAGE] * 2
    assert len(session.calls) == 4  # TLT and GLD never requested
    assert sleep.waits == [DEFAULT_RETRY_WAIT, PACE_SECONDS, DEFAULT_RETRY_WAIT]
    assert [row[5] for row in log_rows(con)[2:]] == [ABORTED_MESSAGE] * 2


def test_isolated_rate_limits_do_not_abort(run, con) -> None:
    limited = [FakeResponse(429, {}), FakeResponse(429, {})]
    results, _ = run(
        {"SPY": list(limited), "QQQ": [ok("QQQ")], "TLT": list(limited), "GLD": [ok("GLD")]}
    )
    assert [r.status for r in results] == ["error", "success", "error", "success"]


@pytest.mark.parametrize(
    ("reply", "message"),
    [
        (FakeResponse(500, {}), "HTTP 500"),
        (FakeResponse(200, bad_json=True), "invalid JSON"),
        (requests.Timeout("read timed out"), "Timeout status=N/A"),
    ],
)
def test_transport_errors(run, con, reply, message) -> None:
    (r,), _ = run({"SPY": [reply]})
    assert (r.status, r.message) == ("error", message)


# --- validation --------------------------------------------------------------------------


def test_null_close_rows_are_dropped_and_reported(run, con) -> None:
    values = rows(4)
    values[1]["close"] = None
    values[2]["close"] = ""
    (r,), _ = run({"SPY": [ok("SPY", values)]})
    assert (r.status, r.rows) == ("success", 2)
    assert r.message == "dropped 2 rows with null close"
    assert log_rows(con)[0][5] == "dropped 2 rows with null close"


@pytest.mark.parametrize("bad_close", ["0", "-1.5", "abc", "nan"])
def test_invalid_close_fails_ticker_without_deleting(run, con, bad_close) -> None:
    seed(con, "SPY")
    values = rows(3)
    values[1]["close"] = bad_close
    (r,), _ = run({"SPY": [ok("SPY", values)]})
    assert r.status == "error" and r.message.startswith("invalid close on")
    assert price_count(con, "SPY") == 5


@pytest.mark.parametrize("kind", ["duplicate", "descending"])
def test_non_ascending_dates_fail_without_deleting(run, con, kind) -> None:
    seed(con, "SPY")
    values = rows(3)
    if kind == "duplicate":
        values[2]["datetime"] = values[1]["datetime"]
    else:
        values.reverse()
    (r,), _ = run({"SPY": [ok("SPY", values)]})
    assert r.status == "error" and "not strictly ascending" in r.message
    assert price_count(con, "SPY") == 5


def test_empty_response_is_no_data_and_deletes_nothing(run, con) -> None:
    seed(con, "SPY")
    (r,), _ = run({"SPY": [ok("SPY", [])]})
    assert (r.status, r.rows) == ("no_data", 0)
    assert price_count(con, "SPY") == 5
    assert log_rows(con)[0][1] == "no_data"


@pytest.mark.parametrize(("age_days", "status"), [(10, "success"), (11, "error")])
def test_freshness_check(run, con, age_days, status) -> None:
    seed(con, "SPY")
    (r,), _ = run({"SPY": [ok("SPY", rows(3, end=TODAY - timedelta(days=age_days)))]})
    assert r.status == status
    if status == "error":
        expected_last = (TODAY - timedelta(days=age_days)).isoformat()
        assert r.message == f"stale data: last date {expected_last}"
        assert price_count(con, "SPY") == 5


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"symbol": "QQQ"}, "invalid meta: symbol=QQQ"),
        ({"currency": "EUR"}, "invalid meta: currency=EUR"),
        ({"exchange": "LSE"}, "invalid meta: exchange=LSE"),
        ({"exchange": None}, "invalid meta: exchange=missing"),
        ({"currency": None}, "invalid meta: currency=missing"),
    ],
)
def test_meta_validation_fails_without_deleting(run, con, overrides, message) -> None:
    seed(con, "SPY")
    (r,), _ = run({"SPY": [ok("SPY", rows(3), **overrides)]})
    assert (r.status, r.message) == ("error", message)
    assert price_count(con, "SPY") == 5


def test_missing_meta_is_error() -> None:
    with pytest.raises(ProviderError, match="invalid meta: missing"):
        parse_time_series({"values": rows(2)}, "SPY")


@pytest.mark.parametrize("exchange", ["NASDAQ", "NYSE Arca", "BATS", "CBOE"])
def test_us_exchanges_accepted(exchange: str) -> None:
    payload = {"meta": meta("SPY", exchange=exchange), "values": rows(1)}
    assert len(parse_time_series(payload, "SPY").bars) == 1


# --- partial bar (today's bar during the session) -----------------------------------------


@pytest.mark.parametrize(
    ("now", "expected_rows", "message"),
    [
        (datetime(2030, 1, 15, 9, 45, tzinfo=NY), 2, "dropped partial bar"),
        (datetime(2030, 1, 15, 16, 29, tzinfo=NY), 2, "dropped partial bar"),
        (datetime(2030, 1, 15, 16, 30, tzinfo=NY), 3, None),
        (datetime(2030, 1, 15, 20, 0, tzinfo=NY), 3, None),
    ],
)
def test_partial_bar_dropped_only_before_1630_new_york(
    con, sleep, tmp_path, now, expected_rows, message
) -> None:
    session = FakeSession({"SPY": [ok("SPY", rows(3, end=TODAY))]})
    provider = make_provider(session, sleep, tmp_path, now=now)
    r = ingest_ticker(con, provider, "SPY")
    assert (r.status, r.rows, r.message) == ("success", expected_rows, message)
    expected_max = TODAY if expected_rows == 3 else TODAY - timedelta(days=1)
    assert r.date_max == expected_max
    assert log_rows(con)[0][5] == message


def test_partial_bar_uses_new_york_date_not_utc(con, sleep, tmp_path) -> None:
    # 2030-01-16 02:00 UTC is still 2030-01-15 21:00 in New York: after the close, keep it.
    now = datetime(2030, 1, 16, 2, 0, tzinfo=UTC)
    session = FakeSession({"SPY": [ok("SPY", rows(3, end=TODAY))]})
    r = ingest_ticker(con, make_provider(session, sleep, tmp_path, now=now), "SPY")
    assert (r.rows, r.message) == (3, None)


def test_previous_day_bars_never_dropped(con, sleep, tmp_path) -> None:
    # Market hours today, but the latest bar is from yesterday: nothing is partial.
    now = datetime(2030, 1, 15, 10, 0, tzinfo=NY)
    session = FakeSession({"SPY": [ok("SPY", rows(3, end=TODAY - timedelta(days=1)))]})
    r = ingest_ticker(con, make_provider(session, sleep, tmp_path, now=now), "SPY")
    assert (r.rows, r.message) == (3, None)


def test_partial_and_null_close_messages_combined(con, sleep, tmp_path) -> None:
    values = rows(4, end=TODAY)
    values[0]["close"] = None
    now = datetime(2030, 1, 15, 11, 0, tzinfo=NY)
    session = FakeSession({"SPY": [ok("SPY", values)]})
    r = ingest_ticker(con, make_provider(session, sleep, tmp_path, now=now), "SPY")
    assert r.rows == 2
    assert r.message == "dropped 1 rows with null close; dropped partial bar"


# --- pacing, dry run, unexpected errors --------------------------------------------------


def test_pacing_between_tickers_only(run, sleep) -> None:
    run({t: [ok(t)] for t in ("SPY", "QQQ", "TLT")})
    assert sleep.waits == [PACE_SECONDS, PACE_SECONDS]


def test_dry_run_writes_nothing(run, con) -> None:
    results, _ = run({"SPY": [ok("SPY", rows(3))]}, dry_run=True)
    assert (results[0].status, results[0].rows) == ("success", 3)
    for table in ("raw_prices", "ingest_log", "dim_asset"):
        assert con.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 0


class ExplodingProvider:
    name = "twelvedata"

    def fetch_daily(self, ticker: str) -> FetchResult:
        raise duckdb.IOException(f"SYNTHETIC failure involving {SECRET}")


def test_unexpected_exception_logs_only_type_name(con) -> None:
    r = ingest_ticker(con, ExplodingProvider(), "SPY", secret=SECRET)
    assert (r.status, r.message) == ("error", "IOException")
    assert log_rows(con)[0][5] == "IOException"


# --- API key never leaks -----------------------------------------------------------------


LEAKY_URL = f"{BASE_URL}?symbol=SPY&apikey={SECRET}"


@pytest.mark.parametrize(
    "exc",
    [
        requests.ConnectionError(f"Max retries exceeded with url: {LEAKY_URL}"),
        requests.HTTPError(f"500 Server Error for url: {LEAKY_URL}"),
    ],
)
def test_api_key_never_in_errors_logs_or_output(con, sleep, tmp_path, capsys, exc) -> None:
    session = FakeSession({"SPY": [exc], "TLT": [api_error(401, f"bad key {SECRET}")]})
    provider = make_provider(session, sleep, tmp_path / "raw")

    with pytest.raises(ProviderError) as info:
        provider.fetch_daily("SPY")
    err = info.value
    assert SECRET not in str(err)
    assert err.__cause__ is None and err.__context__ is None  # requests error not chained

    session.replies["SPY"] = [exc]
    results = ingest_prices(con, provider, ["SPY", "TLT"], sleep=sleep, secret=SECRET)
    print(format_summary(results))
    assert all(r.status == "error" for r in results)
    assert results[1].message == "api_error code=401"  # API message text never copied

    logged = " ".join(str(c) for row in log_rows(con) for c in row)
    out = capsys.readouterr()
    for text in (logged, out.out, out.err, repr(provider), *(r.message for r in results)):
        assert SECRET not in text
    assert not any(SECRET in p.read_text() for p in (tmp_path / "raw").glob("*"))


def test_empty_api_key_rejected() -> None:
    with pytest.raises(ValueError, match="empty"):
        TwelveDataProvider("  ")


def test_redact() -> None:
    assert redact(f"x {SECRET} y", SECRET) == "x *** y"
    assert redact("plain", None) == "plain"


# --- CLI ---------------------------------------------------------------------------------


def test_cli_unknown_ticker_exits_2(capsys) -> None:
    assert main(["--tickers", "SPY,XXX"]) == 2
    assert "XXX" in capsys.readouterr().out


def test_cli_missing_key_exits_2(monkeypatch, capsys) -> None:
    monkeypatch.delenv(prices.API_KEY_ENV, raising=False)
    assert main(["--tickers", "SPY"]) == 2
    assert "TWELVEDATA_API_KEY is not set" in capsys.readouterr().out


def test_cli_runs_with_injected_provider(con, sleep, tmp_path, capsys) -> None:
    session = FakeSession({"SPY": [ok("SPY", rows(3))], "TLT": [api_error(400)]})
    provider = make_provider(session, sleep, tmp_path / "raw")
    code = main(["--tickers", "spy,TLT"], provider=provider, con=con, sleep=sleep)
    assert code == 1  # TLT failed
    out = capsys.readouterr().out
    assert "SPY" in out and "success" in out and "api_error code=400" in out
    assert "100.0" not in out  # no prices printed
    assert con.execute("SELECT count(*) FROM dim_asset").fetchone()[0] == 13
    assert price_count(con, "SPY") == 3
