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


@dataclass(frozen=True, slots=True)
class Finding:
    """One data quality finding, aggregated per (check, entity, severity).

    ``detail`` holds dates and counts only; it must never contain URLs or API keys.
    """

    check: str
    entity: str
    severity: str
    n_affected: int
    detail: str


@dataclass(frozen=True, slots=True)
class PriceAdjustment:
    """Documented correction of provider-adjusted prices (``config/price_adjustments.yaml``).

    OHLC of ``ticker`` before ``ex_date`` are multiplied by ``multiplier`` while the raw
    return on ``ex_date`` is above ``apply_if_raw_return_above``.
    """

    adjustment_id: str
    ticker: str
    ex_date: date
    multiplier: float
    apply_if_raw_return_above: float
    derivation: str
    sources: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class Portfolio:
    """Fixed-weight, long-only portfolio. ``weights`` maps ticker -> weight (sums to 1)."""

    portfolio_id: str
    name: str
    weights: dict[str, float]


@dataclass(frozen=True, slots=True)
class ReleaseOverride:
    """Documented delayed first release: ``obs_date`` became public on ``available_date``."""

    series_id: str
    obs_date: date
    available_date: date
    reason: str
    source: str


@dataclass(frozen=True, slots=True)
class MacroLag:
    """Publication lag of one macro series (``config/macro_lags.yaml``).

    The value dated ``obs_date`` is available on trading days after ``obs_date + lag_days``,
    or after the override's ``available_date`` when one is listed.
    """

    series_id: str
    frequency: str
    lag_days: int
    source: str
    overrides: tuple[ReleaseOverride, ...] = ()
