# SYNTHETIC — not real macro data. Every payload below is a made-up FRED-shaped response
# with obviously fake round values, used only to exercise the ingestion code.
# No test touches the network: HTTP is served by FakeSession.
from collections.abc import Callable, Iterator
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import duckdb
import pytest
import requests

from mrm.ingest import macro
from mrm.ingest.fred import BASE_URL, DEFAULT_RETRY_WAIT, FredClient, parse_observations
from mrm.ingest.macro import PACE_SECONDS, format_summary, ingest_macro, main
from mrm.ingest.providers import ProviderError
from mrm.storage.loaders import upsert_macro
from mrm.storage.models import MacroObservation, MacroSeries

SECRET = "SYNTHETIC-SECRET-KEY"
LAST = date(2030, 1, 31)
DAILY = MacroSeries("DGS10", 90)
FULL = MacroSeries("NFCI", None)


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
    """Serves queued replies per series_id and records every call."""

    def __init__(self, replies: dict[str, list[Reply]]):
        self.replies = {k: list(v) for k, v in replies.items()}
        self.calls: list[dict[str, Any]] = []

    def get(self, url: str, params: dict[str, Any], timeout: float) -> FakeResponse:
        self.calls.append({"url": url, "params": dict(params), "timeout": timeout})
        reply = self.replies[params["series_id"]].pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply


class SleepRecorder:
    def __init__(self) -> None:
        self.waits: list[float] = []

    def __call__(self, seconds: float) -> None:
        self.waits.append(seconds)


def obs(n: int, end: date = LAST, value: str = "1.0") -> list[dict[str, str]]:
    """SYNTHETIC — n consecutive days ending at ``end``, flat fake value."""
    start = end - timedelta(days=n - 1)
    return [
        {
            "realtime_start": "2030-02-01",
            "realtime_end": "2030-02-01",
            "date": (start + timedelta(days=i)).isoformat(),
            "value": value,
        }
        for i in range(n)
    ]


def ok(values: list[dict[str, str]] | None = None, count: int | None = None) -> Reply:
    values = obs(3) if values is None else values
    body = {"count": len(values) if count is None else count, "observations": values}
    return FakeResponse(200, body)


def fred_error(status: int, code: int, text: str = "SYNTHETIC Bad Request") -> Reply:
    return FakeResponse(status, {"error_code": code, "error_message": text})


@pytest.fixture
def sleep() -> SleepRecorder:
    return SleepRecorder()


def make_client(session: FakeSession, sleep: SleepRecorder, raw_dir: Path | None) -> FredClient:
    return FredClient(SECRET, session=session, sleep=sleep, raw_dir=raw_dir)


@pytest.fixture
def run(
    con: duckdb.DuckDBPyConnection, sleep: SleepRecorder, tmp_path: Path
) -> Callable[..., tuple[list[macro.SeriesResult], FakeSession]]:
    """Run ingest_macro against a FakeSession serving ``replies`` keyed by series."""

    def _run(
        replies: dict[MacroSeries, list[Reply]], dry_run: bool = False
    ) -> tuple[list[macro.SeriesResult], FakeSession]:
        session = FakeSession({s.series_id: r for s, r in replies.items()})
        client = make_client(session, sleep, tmp_path / "raw")
        results = ingest_macro(
            con, client, list(replies), sleep=sleep, dry_run=dry_run, secret=SECRET
        )
        return results, session

    return _run


def stored(con: duckdb.DuckDBPyConnection, series_id: str) -> list[tuple]:
    return con.execute(
        "SELECT obs_date, value FROM raw_macro WHERE series_id = ? ORDER BY obs_date",
        [series_id],
    ).fetchall()


def log_rows(con: duckdb.DuckDBPyConnection) -> list[tuple]:
    return con.execute(
        "SELECT source, entity, status, rows_loaded, date_from, date_to, message "
        "FROM ingest_log ORDER BY log_id"
    ).fetchall()


def seed(con: duckdb.DuckDBPyConnection, series_id: str, n: int = 5, end: date = LAST) -> None:
    """SYNTHETIC — pre-existing history that a failed load must not touch."""
    start = end - timedelta(days=n - 1)
    upsert_macro(
        con, [MacroObservation(series_id, start + timedelta(days=i), 9.0) for i in range(n)]
    )


@pytest.fixture(autouse=True)
def no_dotenv(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Never load the developer's real .env during tests."""
    monkeypatch.setattr(macro, "load_dotenv", lambda *a, **k: False)
    yield


# --- happy path, missing values, overlap window ------------------------------------------


def test_first_run_fetches_full_history(run, con, tmp_path) -> None:
    (r,), session = run({DAILY: [ok(obs(4))]})
    assert (r.status, r.rows, r.date_max, r.message) == ("success", 4, LAST, None)
    assert len(stored(con, "DGS10")) == 4
    assert log_rows(con) == [("fred", "DGS10", "success", 4, r.date_min, LAST, None)]
    call = session.calls[0]
    assert call["url"] == BASE_URL and call["timeout"] > 0
    p = call["params"]
    assert (p["file_type"], p["sort_order"]) == ("json", "asc")
    assert "observation_start" not in p
    assert (tmp_path / "raw" / "DGS10.json").exists()


def test_dot_is_stored_as_null_and_counted(run, con) -> None:
    values = obs(4)
    values[1]["value"] = "."
    values[2]["value"] = "."
    (r,), _ = run({DAILY: [ok(values)]})
    assert (r.status, r.rows) == ("success", 4)
    assert r.message == "2 missing values ('.')"
    assert [v for _, v in stored(con, "DGS10")] == [1.0, None, None, 1.0]
    assert log_rows(con)[0][6] == "2 missing values ('.')"


def test_second_run_uses_90_day_overlap_and_revises(run, con) -> None:
    seed(con, "DGS10", n=5)
    (r,), session = run({DAILY: [ok(obs(2, value="2.5"))]})
    assert (
        session.calls[0]["params"]["observation_start"] == (LAST - timedelta(days=90)).isoformat()
    )
    values = [v for _, v in stored(con, "DGS10")]
    assert len(values) == 5  # upsert: no duplicates, older rows kept
    assert values == [9.0, 9.0, 9.0, 2.5, 2.5]  # overlapping values revised


def test_full_reload_series_never_sends_start(run, con) -> None:
    seed(con, "NFCI", n=5)
    (r,), session = run({FULL: [ok(obs(5, value="-0.5"))]})
    assert "observation_start" not in session.calls[0]["params"]
    assert r.message == "full reload"
    assert {v for _, v in stored(con, "NFCI")} == {-0.5}


def test_missing_and_full_reload_notes_combined(run, con) -> None:
    values = obs(3)
    values[0]["value"] = "."
    (r,), _ = run({FULL: [ok(values)]})
    assert r.message == "1 missing values ('.'); full reload"


def test_empty_response_is_no_data(run, con) -> None:
    seed(con, "DGS10")
    (r,), _ = run({DAILY: [ok([])]})
    assert (r.status, r.rows, r.date_min) == ("no_data", 0, None)
    assert len(stored(con, "DGS10")) == 5
    assert log_rows(con)[0][2] == "no_data"


# --- validation --------------------------------------------------------------------------


@pytest.mark.parametrize("bad", ["abc", "nan", "inf", "", None])
def test_invalid_value_fails_series_without_writing(run, con, bad) -> None:
    seed(con, "DGS10")
    values = obs(3, value="1.0")
    values[1]["value"] = bad
    (r,), _ = run({DAILY: [ok(values)]})
    assert r.status == "error" and r.message.startswith("invalid value on")
    assert {v for _, v in stored(con, "DGS10")} == {9.0}


@pytest.mark.parametrize("kind", ["duplicate", "descending"])
def test_non_ascending_dates_fail(run, con, kind) -> None:
    values = obs(3)
    if kind == "duplicate":
        values[2]["date"] = values[1]["date"]
    else:
        values.reverse()
    (r,), _ = run({DAILY: [ok(values)]})
    assert r.status == "error" and "not strictly ascending" in r.message
    assert stored(con, "DGS10") == []


def test_truncated_response_fails(run, con) -> None:
    (r,), _ = run({DAILY: [ok(obs(3), count=5)]})
    assert (r.status, r.message) == ("error", "truncated response")
    assert stored(con, "DGS10") == []


def test_unparsable_date_fails() -> None:
    with pytest.raises(ProviderError, match="unparsable date"):
        parse_observations({"observations": [{"date": "x", "value": "1"}]}, "DGS10")


# --- HTTP / API errors -------------------------------------------------------------------


@pytest.mark.parametrize(
    ("reply", "message"),
    [
        (FakeResponse(500, {}), "HTTP 500"),
        (FakeResponse(200, bad_json=True), "invalid JSON"),
        (FakeResponse(200, [1, 2]), "invalid JSON: not an object"),
        (fred_error(400, 400), "api_error code=400"),
        (requests.Timeout("read timed out"), "Timeout status=N/A"),
    ],
)
def test_transport_errors(run, con, reply, message) -> None:
    seed(con, "DGS10")
    (r,), _ = run({DAILY: [reply]})
    assert (r.status, r.rows, r.message) == ("error", 0, message)
    assert len(stored(con, "DGS10")) == 5
    assert log_rows(con)[0][2:4] == ("error", 0)


@pytest.mark.parametrize("first", [FakeResponse(429, {}), fred_error(429, 429)])
def test_rate_limit_waits_and_retries_once(run, sleep, first) -> None:
    (r,), session = run({DAILY: [first, ok()]})
    assert r.status == "success"
    assert sleep.waits == [DEFAULT_RETRY_WAIT]
    assert len(session.calls) == 2


def test_failing_series_does_not_stop_the_others(run, con, sleep) -> None:
    results, _ = run(
        {
            DAILY: [FakeResponse(429, {}), FakeResponse(429, {})],
            FULL: [ok()],
            MacroSeries("UNRATE", 90): [FakeResponse(500, {})],
            MacroSeries("T10Y2Y", 90): [ok()],
        }
    )
    assert [r.status for r in results] == ["error", "success", "error", "success"]
    assert results[0].message == "HTTP 429"
    assert sleep.waits == [DEFAULT_RETRY_WAIT, PACE_SECONDS, PACE_SECONDS, PACE_SECONDS]
    assert [row[1] for row in log_rows(con)] == ["DGS10", "NFCI", "UNRATE", "T10Y2Y"]


class ExplodingClient:
    name = "fred"

    def fetch_observations(self, series_id: str, start: date | None) -> Any:
        raise duckdb.IOException(f"SYNTHETIC failure involving {SECRET}")


def test_unexpected_exception_logs_only_type_name(con) -> None:
    r = macro.ingest_series(con, ExplodingClient(), DAILY, secret=SECRET)
    assert (r.status, r.message) == ("error", "IOException")


# --- pacing and dry run ------------------------------------------------------------------


def test_pacing_between_series_only(run, sleep) -> None:
    run({MacroSeries(s, 90): [ok()] for s in ("DGS10", "DGS2", "UNRATE")})
    assert sleep.waits == [PACE_SECONDS, PACE_SECONDS]


def test_dry_run_writes_nothing(run, con) -> None:
    results, _ = run({DAILY: [ok(obs(3))]}, dry_run=True)
    assert (results[0].status, results[0].rows) == ("success", 3)
    for table in ("raw_macro", "ingest_log"):
        assert con.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 0


# --- API key never leaks -----------------------------------------------------------------


LEAKY_URL = f"{BASE_URL}?series_id=DGS10&api_key={SECRET}"


@pytest.mark.parametrize(
    "exc",
    [
        requests.ConnectionError(f"Max retries exceeded with url: {LEAKY_URL}"),
        requests.HTTPError(f"500 Server Error for url: {LEAKY_URL}"),
    ],
)
def test_api_key_never_in_errors_logs_or_output(con, sleep, tmp_path, capsys, exc) -> None:
    session = FakeSession({"DGS10": [exc], "NFCI": [fred_error(400, 400, f"bad api_key {SECRET}")]})
    client = make_client(session, sleep, tmp_path / "raw")

    with pytest.raises(ProviderError) as info:
        client.fetch_observations("DGS10", None)
    err = info.value
    assert SECRET not in str(err)
    assert err.__cause__ is None and err.__context__ is None  # requests error not chained

    session.replies["DGS10"] = [exc]
    results = ingest_macro(con, client, [DAILY, FULL], sleep=sleep, secret=SECRET)
    print(format_summary(results))
    assert all(r.status == "error" for r in results)
    assert results[1].message == "api_error code=400"  # FRED's message text never copied

    logged = " ".join(str(c) for row in log_rows(con) for c in row)
    out = capsys.readouterr()
    for text in (logged, out.out, out.err, repr(client), *(r.message for r in results)):
        assert SECRET not in text
    assert not any(SECRET in p.read_text() for p in (tmp_path / "raw").glob("*"))


def test_empty_api_key_rejected() -> None:
    with pytest.raises(ValueError, match="empty"):
        FredClient("  ")


# --- CLI ---------------------------------------------------------------------------------


def test_cli_unknown_series_exits_2(capsys) -> None:
    assert main(["--series", "DGS10,XXX"]) == 2
    assert "XXX" in capsys.readouterr().out


def test_cli_blocked_series_exits_2(capsys) -> None:
    assert main(["--series", "vixcls"]) == 2
    assert "copyrighted" in capsys.readouterr().out


def test_cli_missing_key_exits_2(monkeypatch, capsys) -> None:
    monkeypatch.delenv(macro.API_KEY_ENV, raising=False)
    assert main(["--series", "DGS10"]) == 2
    assert "FRED_API_KEY is not set" in capsys.readouterr().out


def test_cli_runs_with_injected_client(con, sleep, tmp_path, capsys) -> None:
    session = FakeSession({"DGS10": [ok(obs(3, value="4.25"))], "NFCI": [fred_error(400, 400)]})
    client = make_client(session, sleep, tmp_path / "raw")
    code = main(["--series", "dgs10,NFCI"], client=client, con=con, sleep=sleep)
    assert code == 1  # NFCI failed
    out = capsys.readouterr().out
    assert "DGS10" in out and "success" in out and "api_error code=400" in out
    assert "4.25" not in out  # no values printed
    assert len(stored(con, "DGS10")) == 3
