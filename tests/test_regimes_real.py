"""Checks fact_regimes on the REAL local warehouse (not synthetic).

Opens the warehouse read-only. Skipped when the warehouse or a populated fact_regimes table is
absent (CI, or before ``mrm regimes`` has run there). Prints nothing and asserts on counts and
equalities only, never on index values.
"""

from pathlib import Path

import duckdb
import pytest

from mrm.config import load_regime_rules
from mrm.storage.db import get_db_path

DB_PATH = Path(get_db_path())
RULES = load_regime_rules()
EXPECTED_NULL_COLUMNS = {"sahm_gap", "unemployment_rising"}  # Sahm warm-up only


@pytest.fixture
def warehouse() -> duckdb.DuckDBPyConnection:
    if not DB_PATH.exists():
        pytest.skip("local warehouse not available")
    con = duckdb.connect(str(DB_PATH), read_only=True)
    exists = con.execute(
        "SELECT count(*) FROM information_schema.tables WHERE table_name = 'fact_regimes'"
    ).fetchone()[0]
    if not exists or not con.execute("SELECT count(*) FROM fact_regimes").fetchone()[0]:
        con.close()
        pytest.skip("fact_regimes not built in the local warehouse yet (run `mrm regimes`)")
    yield con
    con.close()


def _one(con: duckdb.DuckDBPyConnection, sql: str) -> object:
    return con.execute(sql).fetchone()[0]


def test_one_row_per_prices_clean_day(warehouse: duckdb.DuckDBPyConnection) -> None:
    n_days = _one(warehouse, "SELECT count(DISTINCT trade_date) FROM prices_clean")
    assert _one(warehouse, "SELECT count(*) FROM fact_regimes") == n_days
    assert _one(warehouse, "SELECT count(DISTINCT trade_date) FROM fact_regimes") == n_days
    missing = _one(
        warehouse,
        "SELECT count(*) FROM (SELECT DISTINCT trade_date FROM prices_clean) p "
        "ANTI JOIN fact_regimes f USING (trade_date)",
    )
    assert missing == 0


def test_nulls_only_in_sahm_warmup(warehouse: duckdb.DuckDBPyConnection) -> None:
    columns = [
        r[0]
        for r in warehouse.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = 'fact_regimes' ORDER BY ordinal_position"
        ).fetchall()
    ]
    with_nulls = {
        c for c in columns if _one(warehouse, f"SELECT count(*) - count({c}) FROM fact_regimes")
    }
    assert with_nulls <= EXPECTED_NULL_COLUMNS
    first_gap = _one(
        warehouse, "SELECT min(trade_date) FROM fact_regimes WHERE sahm_gap IS NOT NULL"
    )
    late_nulls = warehouse.execute(
        "SELECT count(*) FROM fact_regimes WHERE trade_date > ? "
        "AND (sahm_gap IS NULL OR unemployment_rising IS NULL)",
        [first_gap],
    ).fetchone()[0]
    assert late_nulls == 0


def test_inputs_equal_macro_asof(warehouse: duckdb.DuckDBPyConnection) -> None:
    mismatches = _one(
        warehouse,
        """
        SELECT count(*) FROM fact_regimes f
        JOIN macro_asof n ON n.trade_date = f.trade_date AND n.series_id = 'NFCI'
        JOIN macro_asof s ON s.trade_date = f.trade_date AND s.series_id = 'STLFSI4'
        JOIN macro_asof c ON c.trade_date = f.trade_date AND c.series_id = 'T10Y2Y'
        JOIN macro_asof u ON u.trade_date = f.trade_date AND u.series_id = 'UNRATE'
        WHERE f.nfci_value IS DISTINCT FROM n.value
           OR f.nfci_value_date IS DISTINCT FROM n.value_date
           OR f.nfci_available_date IS DISTINCT FROM n.available_date
           OR f.nfci_age_days IS DISTINCT FROM n.age_days
           OR f.stlfsi4_value IS DISTINCT FROM s.value
           OR f.t10y2y_value IS DISTINCT FROM c.value
           OR f.unrate_value IS DISTINCT FROM u.value
           OR f.unrate_value_date IS DISTINCT FROM u.value_date
        """,
    )
    assert mismatches == 0


def test_no_value_used_before_its_release(warehouse: duckdb.DuckDBPyConnection) -> None:
    assert (
        _one(warehouse, "SELECT count(*) FROM fact_regimes WHERE nfci_available_date >= trade_date")
        == 0
    )


def test_labels_follow_the_thresholds(warehouse: duckdb.DuckDBPyConnection) -> None:
    r = RULES.regime
    wrong = warehouse.execute(
        "SELECT count(*) FROM fact_regimes WHERE regime IS DISTINCT FROM CASE "
        "WHEN nfci_value >= ? THEN 'stress' WHEN nfci_value >= ? THEN 'tension' "
        "WHEN nfci_value IS NOT NULL THEN 'calm' END",
        [r.stress_at, r.tension_at],
    ).fetchone()[0]
    assert wrong == 0
    backcast_wrong = warehouse.execute(
        "SELECT count(*) FROM fact_regimes WHERE nfci_history IS DISTINCT FROM CASE "
        "WHEN nfci_available_date < ? THEN 'backcast' ELSE 'revised' END",
        [r.first_vintage],
    ).fetchone()[0]
    assert backcast_wrong == 0
