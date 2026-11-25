"""SYNTHETIC — not real market data. A made-up SPY close with three volatility regimes and
made-up NFCI / T10Y2Y paths (seeded), plus a small regime-model config, only to exercise the
HMM regimes v2 quickly."""

from dataclasses import replace
from datetime import date

import duckdb
import numpy as np
import pandas as pd

from mrm.config import load_regime_model
from mrm.regimes.features import build_features
from mrm.storage.loaders import upsert_macro, upsert_prices
from mrm.storage.models import PriceBar, RegimeModelConfig
from tests.regimes_synthetic import DAYS, synthetic_observations

# Small counts so tests run in seconds; vol_window stays the config value (60).
SMALL_CFG: RegimeModelConfig = replace(
    load_regime_model(),
    n_init=2,
    max_iter=50,
    zscore_min_obs=20,
    min_train_days=150,
    refit_every_days=60,
)
# Daily volatility per synthetic regime, and the regime of each block of 80 days.
VOLS = (0.006, 0.012, 0.03)
BLOCKS = (0, 1, 0, 2, 1, 0, 0, 2, 0)


def synthetic_inputs(seed: int = 5) -> tuple[pd.Series, dict[str, pd.Series]]:
    rng = np.random.default_rng(seed)
    states = np.repeat(BLOCKS, 80)
    index = pd.bdate_range("2015-01-02", periods=len(states), name="trade_date")
    returns = rng.normal(0.0003, np.array(VOLS)[states])
    close = pd.Series(100 * np.cumprod(1 + returns), index=index)
    nfci = pd.Series(
        np.convolve(states - 0.8 + rng.normal(0, 0.3, len(states)), np.ones(5) / 5, "same"),
        index=index,
    )
    t10y2y = pd.Series(1.0 - 0.3 * states + rng.normal(0, 0.1, len(states)), index=index)
    return close, {"NFCI": nfci, "T10Y2Y": t10y2y}


def synthetic_features(seed: int = 5, cfg: RegimeModelConfig = SMALL_CFG) -> pd.DataFrame:
    close, macro = synthetic_inputs(seed)
    return build_features(close, macro, cfg.vol_window)


def load_v2_warehouse(
    con: duckdb.DuckDBPyConnection, days: list[date] | None = None, seed: int = 5
) -> None:
    """Load a SYNTHETIC SPY close with volatility regimes on ``days`` (default: the v1
    synthetic calendar) plus the v1 synthetic macro observations."""
    days = DAYS if days is None else days
    rng = np.random.default_rng(seed)
    states = np.resize(np.repeat(BLOCKS, 80), len(days))
    close = 100 * np.cumprod(1 + rng.normal(0.0003, np.array(VOLS)[states]))
    upsert_prices(
        con,
        [
            PriceBar("SPY", d, None, None, None, float(c), 1000)
            for d, c in zip(days, close, strict=True)
        ],
    )
    upsert_macro(con, synthetic_observations())
