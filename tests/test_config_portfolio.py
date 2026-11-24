"""load_portfolio: example file and validation rules (synthetic YAML files)."""

from pathlib import Path

import pytest

from mrm.config import PORTFOLIO_EXAMPLE_PATH, load_portfolio

UNIVERSE = ("SPY", "TLT", "QQQ", "GLD", "EFA", "IEF")


def _write(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "portfolio.yaml"
    path.write_text(body, encoding="utf-8")
    return path


def test_example_portfolio_loads_against_real_universe() -> None:
    portfolio = load_portfolio(PORTFOLIO_EXAMPLE_PATH)
    assert portfolio.portfolio_id == "example"
    assert portfolio.weights == {"SPY": 0.40, "TLT": 0.20, "QQQ": 0.20, "GLD": 0.10, "EFA": 0.10}


def test_tickers_are_upper_cased(tmp_path: Path) -> None:
    path = _write(tmp_path, "portfolio:\n  id: p1\n  holdings:\n    spy: 0.6\n    ief: 0.4\n")
    portfolio = load_portfolio(path, universe=UNIVERSE)
    assert portfolio.weights == {"SPY": 0.6, "IEF": 0.4}
    assert portfolio.name == "p1"


@pytest.mark.parametrize(
    ("holdings", "message"),
    [
        ("    SPY: 0.5\n    XYZ: 0.5\n", "not in config/universe.yaml"),
        ("    SPY: 0.5\n    TLT: 0.4\n", "must sum to 1.0"),
        ("    SPY: 1.2\n    TLT: -0.2\n", "must be >= 0"),
        ("    SPY: abc\n    TLT: 1.0\n", "must be a number"),
        ("    SPY: 0.5\n    spy: 0.5\n", "duplicated ticker"),
    ],
)
def test_invalid_holdings_are_rejected(tmp_path: Path, holdings: str, message: str) -> None:
    path = _write(tmp_path, "portfolio:\n  id: p1\n  holdings:\n" + holdings)
    with pytest.raises(ValueError, match=message):
        load_portfolio(path, universe=UNIVERSE)


@pytest.mark.parametrize("portfolio_id", ["", "My Portfolio", "a:b", "UPPER"])
def test_invalid_id_is_rejected(tmp_path: Path, portfolio_id: str) -> None:
    path = _write(tmp_path, f"portfolio:\n  id: '{portfolio_id}'\n  holdings:\n    SPY: 1.0\n")
    with pytest.raises(ValueError, match=r"portfolio\.id"):
        load_portfolio(path, universe=UNIVERSE)


def test_missing_section_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="'portfolio' mapping"):
        load_portfolio(_write(tmp_path, "holdings: {}\n"), universe=UNIVERSE)
