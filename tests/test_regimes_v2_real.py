"""Checks fact_regimes_v2 on the REAL local warehouse (not synthetic).

Opens the warehouse read-only. Skipped when the warehouse or a populated fact_regimes_v2 table
is absent (CI, or before ``mrm regimes-v2`` has run there). Asserts on counts and equalities
only, never on index values. Expected NULL counts are derived from config/regime_model.yaml,
never hard-coded.
"""

from collections.abc import Iterator
from pathlib import Path

import duckdb
import pytest

from mrm.config import load_regime_model
from mrm.storage.db import get_db_path

DB_PATH = Path(get_db_path())
CFG = load_regime_model()
MODEL_COLUMNS = ("regime", "p_calm", "p_tension", "p_stress", "train_end_date", "train_rows")
PIT = "variant = 'point_in_time'"
FULL = "variant = 'full_sample'"


@pytest.fixture
def warehouse() -> Iterator[duckdb.DuckDBPyConnection]:
    if not DB_PATH.exists():
        pytest.skip("local warehouse not available")
    con = duckdb.connect(str(DB_PATH), read_only=True)
    exists = con.execute(
        "SELECT count(*) FROM information_schema.tables WHERE table_name = 'fact_regimes_v2'"
    ).fetchone()[0]
    if not exists or not con.execute("SELECT count(*) FROM fact_regimes_v2").fetchone()[0]:
        con.close()
        pytest.skip("fact_regimes_v2 not built in the local warehouse yet (run `mrm regimes-v2`)")
    yield con
    con.close()


def _one(con: duckdb.DuckDBPyConnection, sql: str) -> object:
    return con.execute(sql).fetchone()[0]


def test_one_row_per_prices_clean_day_per_variant(warehouse: duckdb.DuckDBPyConnection) -> None:
    n_days = _one(warehouse, "SELECT count(DISTINCT trade_date) FROM prices_clean")
    for where in (PIT, FULL):
        assert _one(warehouse, f"SELECT count(*) FROM fact_regimes_v2 WHERE {where}") == n_days
        missing = _one(
            warehouse,
            "SELECT count(*) FROM (SELECT DISTINCT trade_date FROM prices_clean) p "
            f"ANTI JOIN (SELECT trade_date FROM fact_regimes_v2 WHERE {where}) f "
            "USING (trade_date)",
        )
        assert missing == 0


def test_spy_has_no_gaps_so_the_warmup_formula_applies(
    warehouse: duckdb.DuckDBPyConnection,
) -> None:
    nulls = _one(
        warehouse, f"SELECT count(*) FROM fact_regimes_v2 WHERE {PIT} AND spy_vol_60d IS NULL"
    )
    assert nulls == CFG.vol_window


def test_point_in_time_nulls_only_in_the_warmup(warehouse: duckdb.DuckDBPyConnection) -> None:
    expected = CFG.point_in_time_warmup  # window + zscore_min_obs - 1 + min_train_days
    first_label = _one(
        warehouse, f"SELECT min(trade_date) FROM fact_regimes_v2 WHERE {PIT} AND regime IS NOT NULL"
    )
    for column in MODEL_COLUMNS:
        sql = f"SELECT count(*) FROM fact_regimes_v2 WHERE {PIT} AND {column} IS NULL"
        assert _one(warehouse, sql) == expected, column
        late = warehouse.execute(
            f"SELECT count(*) FROM fact_regimes_v2 WHERE {PIT} AND {column} IS NULL "
            "AND trade_date > ?",
            [first_label],
        ).fetchone()[0]
        assert late == 0, column


def test_full_sample_nulls_only_before_the_first_volatility(
    warehouse: duckdb.DuckDBPyConnection,
) -> None:
    for column in MODEL_COLUMNS:
        sql = f"SELECT count(*) FROM fact_regimes_v2 WHERE {FULL} AND {column} IS NULL"
        assert _one(warehouse, sql) == CFG.vol_window, column


def test_probabilities_sum_to_one_and_label_is_argmax(
    warehouse: duckdb.DuckDBPyConnection,
) -> None:
    bad_sum = _one(
        warehouse,
        "SELECT count(*) FROM fact_regimes_v2 WHERE regime IS NOT NULL "
        "AND abs(p_calm + p_tension + p_stress - 1) > 1e-9",
    )
    assert bad_sum == 0
    not_argmax = _one(
        warehouse,
        "SELECT count(*) FROM fact_regimes_v2 WHERE regime IS NOT NULL AND regime <> "
        "CASE WHEN p_calm >= p_tension AND p_calm >= p_stress THEN 'calm' "
        "WHEN p_tension >= p_stress THEN 'tension' ELSE 'stress' END",
    )
    assert not_argmax == 0


def test_flags_and_training_windows(warehouse: duckdb.DuckDBPyConnection) -> None:
    assert _one(warehouse, f"SELECT bool_and(is_point_in_time) FROM fact_regimes_v2 WHERE {PIT}")
    assert not _one(
        warehouse, f"SELECT bool_or(is_point_in_time) FROM fact_regimes_v2 WHERE {FULL}"
    )
    late_training = _one(
        warehouse,
        f"SELECT count(*) FROM fact_regimes_v2 WHERE {PIT} AND train_end_date >= trade_date",
    )
    assert late_training == 0
    fits = _one(
        warehouse, f"SELECT count(DISTINCT train_end_date) FROM fact_regimes_v2 WHERE {PIT}"
    )
    labelled = _one(
        warehouse, f"SELECT count(*) FROM fact_regimes_v2 WHERE {PIT} AND regime IS NOT NULL"
    )
    assert fits == -(-labelled // CFG.refit_every_days)  # one fit per started 252-day segment


def test_raw_features_equal_macro_asof(warehouse: duckdb.DuckDBPyConnection) -> None:
    for column, series in (("nfci_value", "NFCI"), ("t10y2y_value", "T10Y2Y")):
        mismatches = _one(
            warehouse,
            f"""
            SELECT count(*) FROM fact_regimes_v2 f
            JOIN macro_asof m ON m.trade_date = f.trade_date AND m.series_id = '{series}'
            WHERE f.{column} IS DISTINCT FROM m.value
            """,
        )
        assert mismatches == 0, series
