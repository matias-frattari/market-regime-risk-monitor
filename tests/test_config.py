from pathlib import Path

import pytest

from mrm.config import (
    BLOCKED_SERIES,
    DEFAULT_OVERLAP_DAYS,
    load_macro_series,
    load_universe,
)

EXPECTED_TICKERS = {
    "SPY", "QQQ", "IWM", "EFA", "VWO", "TLT", "IEF", "LQD", "GLD", "XLE", "XLK", "XLF", "XLV",
}  # fmt: skip


def test_universe_loads_13_unique_tickers() -> None:
    assets = load_universe()
    tickers = [a.ticker for a in assets]
    assert len(tickers) == 13
    assert set(tickers) == EXPECTED_TICKERS


def test_universe_asset_classes() -> None:
    by_ticker = {a.ticker: a.asset_class for a in load_universe()}
    assert by_ticker["SPY"] == "equity_us"
    assert by_ticker["VWO"] == "equity_intl"
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


# --- macro series (config/macro.yaml) -------------------------------------------------------


def test_macro_series_loads_with_overlaps() -> None:
    series = {s.series_id: s.overlap_days for s in load_macro_series()}
    assert set(series) == {
        "DGS10", "DGS2", "T10Y2Y", "DGS3MO", "FEDFUNDS", "CPIAUCSL", "UNRATE", "NFCI", "STLFSI4",
    }  # fmt: skip
    assert series["DGS10"] == DEFAULT_OVERLAP_DAYS
    assert series["NFCI"] is None and series["STLFSI4"] is None  # full reload
    assert not set(series) & BLOCKED_SERIES


@pytest.mark.parametrize("blocked", ["VIXCLS", "bamlh0a0hym2"])
def test_macro_rejects_copyrighted_series(tmp_path: Path, blocked: str) -> None:
    path = tmp_path / "macro.yaml"
    path.write_text(f"series:\n  - id: {blocked}\n", encoding="utf-8")
    with pytest.raises(ValueError, match="copyrighted"):
        load_macro_series(path)


@pytest.mark.parametrize(
    ("body", "match"),
    [
        ("series:\n  - id: DGS10\n  - id: dgs10\n", "duplicated"),
        ("series:\n  - id: DGS10\n    overlap_days: -1\n", "overlap_days"),
        ("series:\n  - id: DGS10\n    overlap_days: all\n", "overlap_days"),
        ("series:\n  - id: DGS10\n    overlap_days: true\n", "overlap_days"),
        ("series:\n  - overlap_days: 5\n", "'id'"),
        ("series: []\n", "non-empty"),
        ("- DGS10\n", "non-empty"),
    ],
)
def test_macro_rejects_malformed_file(tmp_path: Path, body: str, match: str) -> None:
    path = tmp_path / "macro.yaml"
    path.write_text(body, encoding="utf-8")
    with pytest.raises(ValueError, match=match):
        load_macro_series(path)


def test_macro_custom_overlap(tmp_path: Path) -> None:
    path = tmp_path / "macro.yaml"
    path.write_text("series:\n  - id: unrate\n    overlap_days: 30\n", encoding="utf-8")
    (s,) = load_macro_series(path)
    assert (s.series_id, s.overlap_days) == ("UNRATE", 30)
