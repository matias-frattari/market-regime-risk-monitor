from pathlib import Path

import pytest

from mrm.config import load_universe

EXPECTED_TICKERS = {
    "SPY", "QQQ", "IWM", "EFA", "EEM", "TLT", "IEF", "LQD", "GLD", "XLE", "XLK", "XLF", "XLV",
}  # fmt: skip


def test_universe_loads_13_unique_tickers() -> None:
    assets = load_universe()
    tickers = [a.ticker for a in assets]
    assert len(tickers) == 13
    assert set(tickers) == EXPECTED_TICKERS


def test_universe_asset_classes() -> None:
    by_ticker = {a.ticker: a.asset_class for a in load_universe()}
    assert by_ticker["SPY"] == "equity_us"
    assert by_ticker["EEM"] == "equity_intl"
    assert by_ticker["TLT"] == "bonds"
    assert by_ticker["GLD"] == "commodities"
    assert by_ticker["XLK"] == "sectors"


def test_universe_rejects_duplicates(tmp_path: Path) -> None:
    path = tmp_path / "universe.yaml"
    path.write_text("equity_us:\n  - SPY\nsectors:\n  - spy\n", encoding="utf-8")
    with pytest.raises(ValueError, match="duplicated ticker"):
        load_universe(path)


def test_universe_rejects_non_list_group(tmp_path: Path) -> None:
    path = tmp_path / "universe.yaml"
    path.write_text("equity_us: SPY\n", encoding="utf-8")
    with pytest.raises(ValueError):
        load_universe(path)
