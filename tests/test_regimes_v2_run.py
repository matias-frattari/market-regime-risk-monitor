# SYNTHETIC — not real market data (see tests/regimes_v2_synthetic.py).
import duckdb
import pytest

from mrm.config import load_regime_rules
from mrm.regimes import run_v2
from mrm.regimes.io import read_regimes_v2
from tests.regimes_synthetic import DAYS
from tests.regimes_v2_synthetic import SMALL_CFG, load_v2_warehouse

RULES = load_regime_rules()


def _count(con: duckdb.DuckDBPyConnection, sql: str) -> int:
    return con.execute(sql).fetchone()[0]


def _run(con: duckdb.DuckDBPyConnection, argv: list[str] | None = None, **kw: object) -> int:
    kw = {"model": SMALL_CFG, "rules": RULES, "adjustments": [], **kw}
    return run_v2.main(argv or [], con=con, **kw)


@pytest.fixture
def loaded(con: duckdb.DuckDBPyConnection) -> duckdb.DuckDBPyConnection:
    load_v2_warehouse(con)
    return con


def test_one_row_per_trading_day_per_variant(loaded: duckdb.DuckDBPyConnection) -> None:
    assert _run(loaded) == 0
    for variant in ("point_in_time", "full_sample"):
        sql = f"FROM fact_regimes_v2 WHERE variant = '{variant}'"
        assert _count(loaded, f"SELECT count(*) {sql}") == len(DAYS)
        assert _count(loaded, f"SELECT count(DISTINCT trade_date) {sql}") == len(DAYS)


def test_nulls_only_in_the_warmup(loaded: duckdb.DuckDBPyConnection) -> None:
    _run(loaded)
    frame = read_regimes_v2(loaded)
    pit = frame[frame["variant"] == "point_in_time"].reset_index(drop=True)
    full = frame[frame["variant"] == "full_sample"].reset_index(drop=True)
    warmup = SMALL_CFG.point_in_time_warmup  # window + zscore_min_obs - 1 + min_train_days
    assert pit["regime"].isna().sum() == warmup
    assert pit["regime"].iloc[warmup:].notna().all()
    assert full["regime"].isna().sum() == SMALL_CFG.vol_window
    assert full["regime"].iloc[SMALL_CFG.vol_window :].notna().all()
    assert not full["is_point_in_time"].any() and pit["is_point_in_time"].all()


def test_dry_run_writes_nothing(
    loaded: duckdb.DuckDBPyConnection, capsys: pytest.CaptureFixture[str]
) -> None:
    assert _run(loaded, ["--dry-run"]) == 0
    assert _count(loaded, "SELECT count(*) FROM fact_regimes_v2") == 0
    assert _count(loaded, "SELECT count(*) FROM fact_regimes") == 0  # v1 only in memory
    assert "dry run: nothing written" in capsys.readouterr().out


def test_rerun_is_idempotent_and_deterministic(loaded: duckdb.DuckDBPyConnection) -> None:
    _run(loaded)
    first = read_regimes_v2(loaded)
    _run(loaded)
    assert read_regimes_v2(loaded).equals(first)


def test_report_sections(
    loaded: duckdb.DuckDBPyConnection, capsys: pytest.CaptureFixture[str]
) -> None:
    _run(loaded)
    out = capsys.readouterr().out
    for text in (
        f"fact_regimes_v2: {2 * len(DAYS)} rows",
        "(one row per day per variant: True)",
        "SPY missing closes: 0",
        "NULL values (variant, column",
        f"point_in_time regime NULL: {SMALL_CFG.point_in_time_warmup}; expected",
        "prediction-only filter step): 0",
        "fits (variant",
        "share of days per regime",
        "regime episodes",
        "regime changes per year",
        "changes on a refit day",
        "v1 rules (rows) x v2 PIT (columns)",
        "agreement per year",
        "dated windows (descriptive only, not targets)",
        "NOT point-in-time",
        "written (full rebuild, one transaction)",
    ):
        assert text in out
    assert "unexpected" not in out


def test_empty_warehouse_exits_1(con: duckdb.DuckDBPyConnection) -> None:
    assert _run(con) == 1


def test_too_short_history_exits_1(
    con: duckdb.DuckDBPyConnection, capsys: pytest.CaptureFixture[str]
) -> None:
    load_v2_warehouse(con, days=DAYS[: SMALL_CFG.point_in_time_warmup])
    assert _run(con) == 1
    assert "no point-in-time fit possible" in capsys.readouterr().out
    assert _count(con, "SELECT count(*) FROM fact_regimes_v2") == 0


def test_invalid_config_exits_2(
    monkeypatch: pytest.MonkeyPatch, con: duckdb.DuckDBPyConnection
) -> None:
    def broken() -> None:
        raise ValueError("model.n_states must be 3")

    monkeypatch.setattr(run_v2, "load_regime_model", broken)
    assert run_v2.main([], con=con, rules=RULES, adjustments=[]) == 2
