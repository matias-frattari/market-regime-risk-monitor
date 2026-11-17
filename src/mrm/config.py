"""Project paths and config-file loaders."""

from pathlib import Path

import yaml

from mrm.storage.models import Asset

REPO_ROOT: Path = Path(__file__).resolve().parents[2]
UNIVERSE_PATH: Path = REPO_ROOT / "config" / "universe.yaml"


def load_universe(path: Path | None = None) -> list[Asset]:
    """Load the ETF universe; each top-level YAML key is the asset class of its tickers.

    Raises ValueError on a malformed file or a duplicated ticker.
    """
    raw = yaml.safe_load((path or UNIVERSE_PATH).read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("universe file must map asset_class -> list of tickers")

    assets: list[Asset] = []
    seen: set[str] = set()
    for asset_class, tickers in raw.items():
        if not isinstance(tickers, list):
            raise ValueError(f"asset class {asset_class!r} must hold a list of tickers")
        for item in tickers:
            ticker = str(item).strip().upper()
            if not ticker:
                raise ValueError(f"empty ticker in asset class {asset_class!r}")
            if ticker in seen:
                raise ValueError(f"duplicated ticker in universe: {ticker}")
            seen.add(ticker)
            assets.append(Asset(ticker=ticker, asset_class=str(asset_class)))
    return assets
