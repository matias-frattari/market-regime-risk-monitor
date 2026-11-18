# SYNTHETIC — not real market data. Prices are made-up round numbers that only mimic the
# shape of the XLF 2016-09-19 problem (a spurious jump on an ex-date) to test prices_clean.
from datetime import date
from pathlib import Path

import duckdb
import pytest

from mrm.config import load_price_adjustments
from mrm.storage.clean import prepare_clean_prices
from mrm.storage.loaders import sync_price_adjustments, upsert_prices
from mrm.storage.models import PriceAdjustment, PriceBar
from mrm.storage.schema import VIEWS

EX = date(2016, 9, 19)
DAYS = [date(2016, 9, 14), date(2016, 9, 15), date(2016, 9, 16), EX, date(2016, 9, 20)]


def adjustment(
    multiplier: float = 1.2,
    threshold: float = 0.10,
    ticker: str = "XLF",
    ex: date = EX,
    aid: str = "a1",
) -> PriceAdjustment:
    return PriceAdjustment(aid, ticker, ex, multiplier, threshold, "SYNTHETIC", ("SYNTHETIC",))


def load(con: duckdb.DuckDBPyConnection, closes: list[float], ticker: str = "XLF") -> None:
    """SYNTHETIC — one bar per day; open/high/low = close, volume 1000."""
    upsert_prices(con, [PriceBar(ticker, d, c, c, c, c, 1000) for d, c in zip(DAYS, closes)])


def clean(con: duckdb.DuckDBPyConnection, ticker: str = "XLF") -> list[tuple]:
    return con.execute(
        "SELECT trade_date, open, high, low, close, volume, adjustment_factor, adjustment_ids "
        "FROM prices_clean WHERE ticker = ? ORDER BY trade_date",
        [ticker],
    ).fetchall()


def status(con: duckdb.DuckDBPyConnection) -> list[tuple]:
    return con.execute(
        "SELECT adjustment_id, raw_return, applied FROM price_adjustment_status ORDER BY 1"
    ).fetchall()


def test_views_exist(con: duckdb.DuckDBPyConnection) -> None:
    names = {
        r[0]
        for r in con.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_type = 'VIEW'"
        ).fetchall()
    }
    assert set(VIEWS) <= names


def test_no_adjustments_prices_clean_equals_raw(con: duckdb.DuckDBPyConnection) -> None:
    load(con, [10.0, 10.1, 10.0, 12.0, 12.0])
    rows = clean(con)
    assert [r[4] for r in rows] == [10.0, 10.1, 10.0, 12.0, 12.0]
    assert all(r[6] == 1.0 and r[7] is None for r in rows)


def test_adjustment_applied_when_raw_jump_above_threshold(con: duckdb.DuckDBPyConnection) -> None:
    # Spurious +20% jump on the ex-date; true return is ~0%.
    load(con, [10.0, 10.1, 10.0, 12.0, 12.06])
    load(con, [50.0] * 5, ticker="SPY")  # another ticker must stay untouched
    sync_price_adjustments(con, [adjustment(multiplier=1.2)])

    ((aid, raw_return, applied),) = status(con)
    assert applied and raw_return == pytest.approx(0.20)
    rows = clean(con)
    before, ex, after = rows[2], rows[3], rows[4]
    assert before[1:5] == pytest.approx((12.0, 12.0, 12.0, 12.0))  # all OHLC x1.2
    assert before[6] == pytest.approx(1.2) and before[7] == "a1"
    assert ex[4] / before[4] - 1 == pytest.approx(0.0)  # return on ex-date now ~0
    assert (ex[4], ex[6], ex[7]) == (12.0, 1.0, None)  # ex-date and later untouched
    assert after[4] == 12.06
    assert all(r[5] == 1000 for r in rows)  # volume never corrected
    assert [r[4] for r in clean(con, "SPY")] == [50.0] * 5


def test_adjustment_not_applied_when_provider_fixed(con: duckdb.DuckDBPyConnection) -> None:
    # Provider already continuous on the ex-date (+0.6%): applying would double-adjust.
    load(con, [10.0, 10.1, 10.0, 10.06, 10.1])
    sync_price_adjustments(con, [adjustment(multiplier=1.2)])
    ((_, raw_return, applied),) = status(con)
    assert not applied and raw_return == pytest.approx(0.006)
    assert [r[4] for r in clean(con)] == [10.0, 10.1, 10.0, 10.06, 10.1]
    assert all(r[6] == 1.0 for r in clean(con))


def test_missing_ex_date_is_not_applied(con: duckdb.DuckDBPyConnection) -> None:
    load(con, [10.0, 10.1, 10.0, 12.0, 12.0])
    con.execute("DELETE FROM raw_prices WHERE trade_date = ?", [EX])
    sync_price_adjustments(con, [adjustment()])
    ((_, raw_return, applied),) = status(con)
    assert raw_return is None and applied is False
    assert all(r[6] == 1.0 for r in clean(con))


def test_two_applied_adjustments_multiply(con: duckdb.DuckDBPyConnection) -> None:
    # Jumps of +20% on 09-15 and +50% on EX.
    load(con, [10.0, 12.0, 12.0, 18.0, 18.0])
    sync_price_adjustments(
        con,
        [
            adjustment(multiplier=1.5, aid="late"),
            adjustment(multiplier=1.2, ex=date(2016, 9, 15), aid="early"),
        ],
    )
    rows = clean(con)
    assert rows[0][6] == pytest.approx(1.8) and rows[0][7] == "early,late"
    assert rows[1][6] == pytest.approx(1.5) and rows[1][7] == "late"
    assert [r[4] for r in rows] == pytest.approx([18.0] * 5)


def test_sync_replaces_config_and_is_idempotent(con: duckdb.DuckDBPyConnection) -> None:
    sync_price_adjustments(con, [adjustment(aid="a1"), adjustment(aid="a2")])
    sync_price_adjustments(con, [adjustment(aid="a2")])
    sync_price_adjustments(con, [adjustment(aid="a2")])
    assert con.execute("SELECT adjustment_id FROM price_adjustments").fetchall() == [("a2",)]
    sync_price_adjustments(con, [])  # removing the entry from the yaml reverts the correction
    assert con.execute("SELECT count(*) FROM price_adjustments").fetchone()[0] == 0


def test_prepare_clean_prices_loads_repo_config(con: duckdb.DuckDBPyConnection) -> None:
    assert prepare_clean_prices(con) == 1
    row = con.execute("SELECT ticker, ex_date, multiplier FROM price_adjustments").fetchone()
    assert row == ("XLF", EX, 1.18729)


# --- config loader -------------------------------------------------------------------------

VALID = """
adjustments:
  - id: x1
    ticker: xlf
    ex_date: 2016-09-19
    multiplier: 1.18729
    apply_if_raw_return_above: 0.10
    derivation: "SYNTHETIC derivation"
    sources: ["https://example.invalid/source"]
"""


def test_loader_parses_valid(tmp_path: Path) -> None:
    path = tmp_path / "adj.yaml"
    path.write_text(VALID, encoding="utf-8")
    (item,) = load_price_adjustments(path)
    assert (item.ticker, item.ex_date, item.multiplier) == ("XLF", EX, 1.18729)


def test_loader_accepts_empty_list(tmp_path: Path) -> None:
    path = tmp_path / "adj.yaml"
    path.write_text("adjustments: []\n", encoding="utf-8")
    assert load_price_adjustments(path) == []


@pytest.mark.parametrize(
    ("old", "new", "match"),
    [
        ("multiplier: 1.18729", "multiplier: 0", "multiplier"),
        ("apply_if_raw_return_above: 0.10", "apply_if_raw_return_above: -0.1", "apply_if"),
        ('derivation: "SYNTHETIC derivation"', 'derivation: ""', "derivation"),
        ('sources: ["https://example.invalid/source"]', "sources: []", "sources"),
        ("ex_date: 2016-09-19", "ex_date: someday", "ex_date"),
        ("ticker: xlf", "ticker: ''", "ticker"),
    ],
)
def test_loader_rejects_malformed(tmp_path: Path, old: str, new: str, match: str) -> None:
    assert old in VALID
    path = tmp_path / "adj.yaml"
    path.write_text(VALID.replace(old, new), encoding="utf-8")
    with pytest.raises(ValueError, match=match):
        load_price_adjustments(path)


def test_loader_rejects_duplicated_id(tmp_path: Path) -> None:
    path = tmp_path / "adj.yaml"
    path.write_text(VALID + VALID.split("adjustments:\n")[1], encoding="utf-8")
    with pytest.raises(ValueError, match="duplicated"):
        load_price_adjustments(path)
