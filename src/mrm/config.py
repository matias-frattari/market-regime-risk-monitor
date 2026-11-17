"""Project paths and config-file loaders."""

from pathlib import Path

import yaml

from mrm.storage.models import Asset, MacroSeries

REPO_ROOT: Path = Path(__file__).resolve().parents[2]
UNIVERSE_PATH: Path = REPO_ROOT / "config" / "universe.yaml"
MACRO_PATH: Path = REPO_ROOT / "config" / "macro.yaml"
DEFAULT_OVERLAP_DAYS = 90
FULL_RELOAD = "full"
# Copyrighted on FRED (Cboe, ICE): never ingested. See docs/decisions/0002.
BLOCKED_SERIES: frozenset[str] = frozenset({"VIXCLS", "BAMLH0A0HYM2"})


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


def _parse_overlap(value: object, series_id: str) -> int | None:
    if value is None:
        return DEFAULT_OVERLAP_DAYS
    if value == FULL_RELOAD:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"overlap_days of {series_id} must be an integer >= 0 or 'full'")
    return value


def load_macro_series(path: Path | None = None) -> list[MacroSeries]:
    """Load the FRED series list (``series: [{id, overlap_days}]``).

    Raises ValueError on a malformed file, a duplicated id or a blocked (copyrighted) id.
    """
    raw = yaml.safe_load((path or MACRO_PATH).read_text(encoding="utf-8"))
    entries = raw.get("series") if isinstance(raw, dict) else None
    if not isinstance(entries, list) or not entries:
        raise ValueError("macro file must hold a non-empty 'series' list")

    result: list[MacroSeries] = []
    seen: set[str] = set()
    for entry in entries:
        if not isinstance(entry, dict) or not str(entry.get("id") or "").strip():
            raise ValueError("each macro series needs an 'id'")
        series_id = str(entry["id"]).strip().upper()
        if series_id in BLOCKED_SERIES:
            raise ValueError(f"series {series_id} is copyrighted and must not be ingested")
        if series_id in seen:
            raise ValueError(f"duplicated macro series: {series_id}")
        seen.add(series_id)
        result.append(MacroSeries(series_id, _parse_overlap(entry.get("overlap_days"), series_id)))
    return result
