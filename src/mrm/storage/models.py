"""Typed row containers passed to the storage loaders."""

from dataclasses import dataclass
from datetime import date


@dataclass(frozen=True, slots=True)
class Asset:
    """One ticker of the investable universe."""

    ticker: str
    asset_class: str


@dataclass(frozen=True, slots=True)
class PriceBar:
    """One daily, split/dividend-adjusted OHLCV bar for one ticker."""

    ticker: str
    trade_date: date
    open: float | None
    high: float | None
    low: float | None
    close: float
    volume: int | None


@dataclass(frozen=True, slots=True)
class MacroObservation:
    """One observation of a macro series (FRED). ``value`` is None when FRED reports '.'."""

    series_id: str
    obs_date: date
    value: float | None


@dataclass(frozen=True, slots=True)
class MacroSeries:
    """One FRED series to ingest. ``overlap_days`` None means a full-history reload."""

    series_id: str
    overlap_days: int | None
