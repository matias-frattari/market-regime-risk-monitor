"""Validates the rule-based NYSE calendar against the real SPY dates in the local warehouse.

Not synthetic: reads `raw_prices` read-only. Skipped when the warehouse is absent (CI).
"""

from datetime import date
from pathlib import Path

import duckdb
import pytest

from mrm.config import load_data_quality_config
from mrm.quality.calendar import nyse_business_days
from mrm.storage.db import get_db_path

DB_PATH = Path(get_db_path())

# Trading days by rule that SPY does not have: every one is a documented one-off closure.
EXPECTED_MISSING_IN_SPY = {
    date(2012, 10, 29),  # Hurricane Sandy
    date(2012, 10, 30),  # Hurricane Sandy
    date(2018, 12, 5),  # National day of mourning, G. H. W. Bush
    date(2025, 1, 9),  # National day of mourning, Carter
}
# SPY dates that are not trading days by rule: none expected.
EXPECTED_EXTRA_IN_SPY: set[date] = set()


def _spy_dates() -> set[date]:
    con = duckdb.connect(str(DB_PATH), read_only=True)
    try:
        rows = con.execute("SELECT trade_date FROM raw_prices WHERE ticker = 'SPY'").fetchall()
    finally:
        con.close()
    return {r[0] for r in rows}


@pytest.mark.skipif(not DB_PATH.exists(), reason="local warehouse not available")
def test_rules_match_real_spy_dates() -> None:
    spy = _spy_dates()
    if not spy:
        pytest.skip("no SPY prices in the warehouse")
    by_rule = set(nyse_business_days(min(spy), max(spy)))

    assert spy - by_rule == EXPECTED_EXTRA_IN_SPY
    assert by_rule - spy == EXPECTED_MISSING_IN_SPY

    closures = set(load_data_quality_config().special_closures)
    assert EXPECTED_MISSING_IN_SPY <= closures
    with_closures = set(nyse_business_days(min(spy), max(spy), closures))
    assert with_closures == spy
