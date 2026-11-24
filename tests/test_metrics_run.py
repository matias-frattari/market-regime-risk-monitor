# SYNTHETIC — not real market data. Prices are a seeded random walk and DGS3MO a made-up
# constant, on NYSE trading days, only to exercise the metrics runner end to end.
from datetime import date, timedelta

import duckdb
import numpy as np
import pandas as pd
import pytest

from mrm.metrics.pipeline import compute_metrics
from mrm.metrics.run import main
from mrm.quality.calendar import nyse_business_days
from mrm.storage.loaders import upsert_macro, upsert_prices
from mrm.storage.models import MacroObservation, Portfolio, PriceBar

N_DAYS = 200
TICKERS = ("SPY", "TLT", "GLD")
P1 = Portfolio("p1", "Synthetic 60/40", {"SPY": 0.6, "TLT": 0.4})
P2 = Portfolio("p2", "Synthetic gold", {"GLD": 1.0})
FULL_METRICS = {
    "ann_return", "ann_vol", "max_drawdown", "sharpe",
    "var_hist_95", "cvar_hist_95", "var_param_95", "cvar_param_95",
}  # fmt: skip
DATES = nyse_business_days(date(2024, 1, 2), date(2025, 1, 31))[:N_DAYS]
RF_START = 10  # DGS3MO has no observation before DATES[RF_START] -> NULL on early days


def _closes(seed: int) -> np.ndarray:
    rng = np.random.default_rng(seed)
    return 100.0 * np.exp(np.cumsum(rng.normal(0.0003, 0.01, N_DAYS)))


def load_synthetic(con: duckdb.DuckDBPyConnection) -> None:
    bars = [
        PriceBar(t, d, None, None, None, float(c), None)
        for i, t in enumerate(TICKERS)
        for d, c in zip(DATES, _closes(i), strict=True)
    ]
    upsert_prices(con, bars)
    obs = [MacroObservation("DGS3MO", d, 5.0) for d in DATES[RF_START:]]
    obs[20] = MacroObservation("DGS3MO", obs[20].obs_date, None)  # FRED '.' -> forward-filled
    upsert_macro(con, obs)


@pytest.fixture
def loaded(con: duckdb.DuckDBPyConnection) -> duckdb.DuckDBPyConnection:
    load_synthetic(con)
    return con


def _count(con: duckdb.DuckDBPyConnection, sql: str, params: list | None = None) -> int:
    return con.execute(sql, params or []).fetchone()[0]


def test_run_writes_expected_row_counts(loaded: duckdb.DuckDBPyConnection) -> None:
    assert main([], con=loaded, portfolio=P1, adjustments=[]) == 0
    n_ret, n_win = N_DAYS - 1, N_DAYS - 1 - 125
    assert _count(loaded, "SELECT count(*) FROM fact_returns") == 4 * n_ret
    assert _count(loaded, "SELECT count(*) FROM fact_correlations") == 3 * n_win
    assert (
        _count(loaded, "SELECT count(*) FROM fact_risk_metrics WHERE entity_type = 'holding'")
        == 2 * 2 * n_win
    )
    holdings = loaded.execute(
        "SELECT DISTINCT entity_id FROM fact_risk_metrics WHERE entity_type = 'holding' ORDER BY 1"
    ).fetchall()
    assert holdings == [("p1:SPY",), ("p1:TLT",)]
    full = loaded.execute(
        "SELECT entity_id, list(metric) FROM fact_risk_metrics WHERE window_label = 'full' "
        "GROUP BY 1"
    ).fetchall()
    assert {e for e, _ in full} == {*TICKERS, "p1"}
    assert all(set(metrics) == FULL_METRICS for _, metrics in full)


def test_off_calendar_row_is_discarded_and_counted(
    loaded: duckdb.DuckDBPyConnection, capsys: pytest.CaptureFixture[str]
) -> None:
    # One stray Saturday row in GLD (not a holding) must not break the other tickers.
    saturday = next(
        d for d in (DATES[100] + timedelta(days=i) for i in range(7)) if d.weekday() == 5
    )
    upsert_prices(loaded, [PriceBar("GLD", saturday, None, None, None, 100.0, None)])
    assert main([], con=loaded, portfolio=P1, adjustments=[]) == 0
    assert "off-calendar rows discarded (not NYSE trading days): 1" in capsys.readouterr().out
    n_win = N_DAYS - 1 - 125
    assert _count(loaded, "SELECT count(*) FROM fact_correlations") == 3 * n_win
    assert _count(
        loaded, "SELECT count(*) FROM fact_risk_metrics WHERE metric = 'vol_60d'"
    ) == 4 * (N_DAYS - 60)
    assert _count(loaded, "SELECT count(*) FROM fact_returns WHERE trade_date = ?", [saturday]) == 0


def test_sharpe_excludes_null_risk_free_days(loaded: duckdb.DuckDBPyConnection) -> None:
    main([], con=loaded, portfolio=P1, adjustments=[])
    n_obs = loaded.execute(
        "SELECT metric, n_obs FROM fact_risk_metrics WHERE entity_id = 'p1' "
        "AND metric IN ('sharpe', 'ann_vol')"
    ).fetchall()
    # macro_asof uses obs_date < trade_date: rf is NULL up to and including DATES[RF_START]
    assert dict(n_obs) == {"ann_vol": N_DAYS - 1, "sharpe": N_DAYS - 1 - RF_START}


def test_rolling_euler_sums_to_portfolio_vol(loaded: duckdb.DuckDBPyConnection) -> None:
    main([], con=loaded, portfolio=P1, adjustments=[])
    gaps = loaded.execute(
        """
        WITH rc AS (
            SELECT as_of_date, sum(value) AS total FROM fact_risk_metrics
            WHERE metric = 'risk_contrib' GROUP BY 1
        ),
        port AS (
            SELECT trade_date, stddev_samp(simple_return) OVER (
                ORDER BY trade_date ROWS BETWEEN 125 PRECEDING AND CURRENT ROW) * sqrt(252) AS vol
            FROM fact_returns WHERE entity_id = 'p1'
        )
        SELECT max(abs(rc.total - port.vol)) FROM rc JOIN port ON port.trade_date = rc.as_of_date
        """
    ).fetchone()[0]
    assert gaps < 1e-12


def test_rerun_is_idempotent_and_keeps_other_portfolios(
    loaded: duckdb.DuckDBPyConnection,
) -> None:
    main([], con=loaded, portfolio=P1, adjustments=[])
    main([], con=loaded, portfolio=P2, adjustments=[])
    before = _count(loaded, "SELECT count(*) FROM fact_risk_metrics")
    main([], con=loaded, portfolio=P1, adjustments=[])
    assert _count(loaded, "SELECT count(*) FROM fact_risk_metrics") == before
    entities = {
        r[0]
        for r in loaded.execute(
            "SELECT DISTINCT entity_id FROM fact_returns WHERE entity_type = 'portfolio'"
        ).fetchall()
    }
    assert entities == {"p1", "p2"}
    assert _count(loaded, "SELECT count(*) FROM fact_returns") == 5 * (N_DAYS - 1)


def test_dry_run_writes_nothing_and_prints_no_prices(
    loaded: duckdb.DuckDBPyConnection, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["--dry-run"], con=loaded, portfolio=P1, adjustments=[]) == 0
    assert _count(loaded, "SELECT count(*) FROM fact_returns") == 0
    out = capsys.readouterr().out
    assert "fact_correlations" in out and "dry run" in out
    last_close = f"{_closes(0)[-1]:.2f}"
    assert last_close not in out


def test_missing_holding_or_empty_prices_exit_1(con: duckdb.DuckDBPyConnection) -> None:
    assert main([], con=con, portfolio=P1, adjustments=[]) == 1  # empty prices_clean
    upsert_prices(con, [PriceBar("SPY", date(2024, 1, 2), None, None, None, 100.0, None)])
    assert main([], con=con, portfolio=P1, adjustments=[]) == 1  # TLT has no prices


def test_compute_metrics_is_pure_and_matches_written_rows(
    loaded: duckdb.DuckDBPyConnection,
) -> None:
    prices = pd.DataFrame(
        {t: _closes(i) for i, t in enumerate(TICKERS)}, index=pd.DatetimeIndex(DATES)
    )
    result = compute_metrics(prices, pd.Series(np.nan, index=prices.index), P1)
    assert "sharpe" not in set(result.risk_metrics.metric)  # no risk-free -> no Sharpe row
    assert result.risk_metrics.value.notna().all()
    assert len(result.returns) == 4 * (N_DAYS - 1)
