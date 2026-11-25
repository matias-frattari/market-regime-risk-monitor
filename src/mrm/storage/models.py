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


@dataclass(frozen=True, slots=True)
class LevelRule:
    """Three-level classification of a stress index (``config/regime_rules.yaml``).

    ``value < tension_at`` -> calm, ``tension_at <= value < stress_at`` -> tension,
    ``value >= stress_at`` -> stress. A value whose ``available_date`` is before
    ``first_vintage`` is a backcast (the index did not exist in real time).
    """

    signal: str
    tension_at: float
    stress_at: float
    first_vintage: date
    vintage_source: str


@dataclass(frozen=True, slots=True)
class SahmRule:
    """Sahm-rule flag: ``ma_months`` average minus its minimum over the previous
    ``lookback_months`` monthly averages, flagged when ``>= threshold_pp``."""

    signal: str
    ma_months: int
    ma_min_obs: int
    lookback_months: int
    lookback_min_obs: int
    threshold_pp: float


@dataclass(frozen=True, slots=True)
class KnownEvent:
    """Dated event used only to label report lines (e.g. stale NFCI runs)."""

    name: str
    start: date
    end: date
    source: str


@dataclass(frozen=True, slots=True)
class RegimeRules:
    """Rules of the v1 regime classification (``config/regime_rules.yaml``, ADR 0006)."""

    regime: LevelRule
    robustness: LevelRule
    curve_signal: str
    curve_below: float
    sahm: SahmRule
    nfci_stale_days: int
    known_events: tuple[KnownEvent, ...] = ()


@dataclass(frozen=True, slots=True)
class RegimeModelConfig:
    """Hyperparameters of the HMM regimes v2 (``config/regime_model.yaml``, ADR 0007).

    Fixed a priori: never tuned on returns, backtests or the v1/v2 comparison.
    """

    n_states: int
    covariance_type: str
    n_init: int
    seed: int
    max_iter: int
    tol: float
    vol_ticker: str
    vol_window: int
    macro_features: tuple[str, ...]
    zscore_min_obs: int
    min_train_days: int
    refit_every_days: int
    windows: tuple[KnownEvent, ...] = ()

    @property
    def point_in_time_warmup(self) -> int:
        """Leading trading days without a point-in-time label when SPY has no gaps: the
        volatility window, then ``zscore_min_obs - 1`` more rows (the z-score is known from
        its ``zscore_min_obs``-th value), then ``min_train_days`` valid rows before the first fit.
        """
        return self.vol_window + (self.zscore_min_obs - 1) + self.min_train_days
