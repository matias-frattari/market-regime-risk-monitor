"""Project paths and config-file loaders."""

import math
import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import yaml

from mrm.storage.models import (
    Asset,
    KnownEvent,
    LevelRule,
    MacroLag,
    MacroSeries,
    Portfolio,
    PriceAdjustment,
    RegimeRules,
    ReleaseOverride,
    SahmRule,
)

REPO_ROOT: Path = Path(__file__).resolve().parents[2]
UNIVERSE_PATH: Path = REPO_ROOT / "config" / "universe.yaml"
MACRO_PATH: Path = REPO_ROOT / "config" / "macro.yaml"
DATA_QUALITY_PATH: Path = REPO_ROOT / "config" / "data_quality.yaml"
PRICE_ADJUSTMENTS_PATH: Path = REPO_ROOT / "config" / "price_adjustments.yaml"
MACRO_LAGS_PATH: Path = REPO_ROOT / "config" / "macro_lags.yaml"
REGIME_RULES_PATH: Path = REPO_ROOT / "config" / "regime_rules.yaml"
PORTFOLIO_PATH: Path = REPO_ROOT / "config" / "portfolio.yaml"  # personal, gitignored
PORTFOLIO_EXAMPLE_PATH: Path = REPO_ROOT / "config" / "portfolio.example.yaml"
WEIGHT_SUM_TOLERANCE = 1e-9
_PORTFOLIO_ID = re.compile(r"[a-z0-9_-]+")
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


FREQUENCIES: tuple[str, ...] = ("daily", "weekly", "monthly")


@dataclass(frozen=True, slots=True)
class ReturnException:
    """Known stress window: extreme returns inside it are info, not warnings.

    ``tickers`` empty means every ticker.
    """

    start: date
    end: date
    reason: str
    tickers: frozenset[str] = frozenset()

    def covers(self, ticker: str, day: date) -> bool:
        return self.start <= day <= self.end and (not self.tickers or ticker in self.tickers)


@dataclass(frozen=True, slots=True)
class DataQualityConfig:
    """Thresholds and documented exceptions of ``config/data_quality.yaml``."""

    special_closures: dict[date, str]
    max_missing_gap_days: int
    extreme_return_abs: float
    extreme_return_top_n: int
    stale_after_business_days: int
    known_extreme_returns: tuple[ReturnException, ...]
    series_frequency: dict[str, str]
    max_age_days: dict[str, int]
    classify_gaps_from: date
    known_gaps: dict[tuple[str, date], str]


def _section(raw: dict[str, Any], key: str) -> dict[str, Any]:
    value = raw.get(key)
    if not isinstance(value, dict):
        raise ValueError(f"config needs a '{key}' mapping")
    return value


def _date(value: object, where: str) -> date:
    if not isinstance(value, date):
        raise ValueError(f"{where} must be a YYYY-MM-DD date")
    return value


def _int(value: object, where: str, minimum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ValueError(f"{where} must be an integer >= {minimum}")
    return value


def _reason(entry: dict[str, Any], where: str) -> str:
    reason = str(entry.get("reason") or "").strip()
    if not reason:
        raise ValueError(f"{where} needs a 'reason'")
    return reason


def _entries(section: dict[str, Any], key: str) -> list[dict[str, Any]]:
    entries = section.get(key) or []
    if not isinstance(entries, list) or not all(isinstance(e, dict) for e in entries):
        raise ValueError(f"'{key}' must be a list of mappings")
    return entries


def load_data_quality_config(path: Path | None = None) -> DataQualityConfig:
    """Load ``config/data_quality.yaml``. Raises ValueError on a malformed file."""
    raw = yaml.safe_load((path or DATA_QUALITY_PATH).read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("data quality config must be a mapping")
    calendar, prices, macro = (_section(raw, k) for k in ("calendar", "prices", "macro"))

    closures: dict[date, str] = {}
    for entry in _entries(calendar, "special_closures"):
        day = _date(entry.get("date"), "special_closures.date")
        closures[day] = _reason(entry, f"special closure {day}")

    threshold = prices.get("extreme_return_abs")
    if isinstance(threshold, bool) or not isinstance(threshold, int | float) or threshold <= 0:
        raise ValueError("prices.extreme_return_abs must be a number > 0")

    exceptions: list[ReturnException] = []
    for entry in _entries(prices, "known_extreme_returns"):
        start = _date(entry.get("from"), "known_extreme_returns.from")
        end = _date(entry.get("to"), "known_extreme_returns.to")
        if end < start:
            raise ValueError(f"known_extreme_returns window {start}..{end} ends before it starts")
        tickers = frozenset(str(t).strip().upper() for t in entry.get("tickers") or [])
        exceptions.append(ReturnException(start, end, _reason(entry, f"window {start}"), tickers))

    frequency = macro.get("series_frequency")
    if not isinstance(frequency, dict) or not frequency:
        raise ValueError("macro.series_frequency must map series id -> frequency")
    bad = {k: v for k, v in frequency.items() if v not in FREQUENCIES}
    if bad:
        raise ValueError(f"unknown frequency in macro.series_frequency: {bad}")
    max_age = macro.get("max_age_days")
    if not isinstance(max_age, dict) or set(max_age) != set(FREQUENCIES):
        raise ValueError(f"macro.max_age_days must define exactly {', '.join(FREQUENCIES)}")

    gaps: dict[tuple[str, date], str] = {}
    for entry in _entries(macro, "known_gaps"):
        series_id = str(entry.get("series") or "").strip().upper()
        if not series_id:
            raise ValueError("each known gap needs a 'series'")
        day = _date(entry.get("date"), f"known gap of {series_id}")
        gaps[(series_id, day)] = _reason(entry, f"known gap {series_id} {day}")

    return DataQualityConfig(
        special_closures=closures,
        max_missing_gap_days=_int(prices.get("max_missing_gap_days"), "max_missing_gap_days", 1),
        extreme_return_abs=float(threshold),
        extreme_return_top_n=_int(prices.get("extreme_return_top_n"), "extreme_return_top_n", 1),
        stale_after_business_days=_int(
            prices.get("stale_after_business_days"), "stale_after_business_days", 0
        ),
        known_extreme_returns=tuple(exceptions),
        series_frequency={str(k).upper(): v for k, v in frequency.items()},
        max_age_days={k: _int(v, f"max_age_days.{k}", 1) for k, v in max_age.items()},
        classify_gaps_from=_date(macro.get("classify_gaps_from"), "macro.classify_gaps_from"),
        known_gaps=gaps,
    )


def _positive_number(value: object, where: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float) or not value > 0:
        raise ValueError(f"{where} must be a number > 0")
    return float(value)


def load_price_adjustments(path: Path | None = None) -> list[PriceAdjustment]:
    """Load ``config/price_adjustments.yaml`` (``adjustments: [...]``, may be empty).

    Raises ValueError on a malformed entry, a duplicated id, or a missing derivation/source.
    """
    raw = yaml.safe_load((path or PRICE_ADJUSTMENTS_PATH).read_text(encoding="utf-8"))
    if not isinstance(raw, dict) or "adjustments" not in raw:
        raise ValueError("price adjustments file must hold an 'adjustments' list")
    entries = _entries(raw, "adjustments")

    result: list[PriceAdjustment] = []
    seen: set[str] = set()
    for entry in entries:
        adjustment_id = str(entry.get("id") or "").strip()
        if not adjustment_id:
            raise ValueError("each price adjustment needs an 'id'")
        if adjustment_id in seen:
            raise ValueError(f"duplicated price adjustment id: {adjustment_id}")
        seen.add(adjustment_id)
        ticker = str(entry.get("ticker") or "").strip().upper()
        if not ticker:
            raise ValueError(f"price adjustment {adjustment_id} needs a 'ticker'")
        derivation = str(entry.get("derivation") or "").strip()
        if not derivation:
            raise ValueError(f"price adjustment {adjustment_id} needs a 'derivation'")
        sources = entry.get("sources")
        if not isinstance(sources, list) or not all(str(s).strip() for s in sources) or not sources:
            raise ValueError(f"price adjustment {adjustment_id} needs a non-empty 'sources' list")
        result.append(
            PriceAdjustment(
                adjustment_id=adjustment_id,
                ticker=ticker,
                ex_date=_date(entry.get("ex_date"), f"{adjustment_id}.ex_date"),
                multiplier=_positive_number(entry.get("multiplier"), f"{adjustment_id}.multiplier"),
                apply_if_raw_return_above=_positive_number(
                    entry.get("apply_if_raw_return_above"),
                    f"{adjustment_id}.apply_if_raw_return_above",
                ),
                derivation=derivation,
                sources=tuple(str(s).strip() for s in sources),
            )
        )
    return result


def _text(entry: dict[str, Any], key: str, where: str) -> str:
    text = str(entry.get(key) or "").strip()
    if not text:
        raise ValueError(f"{where} needs a '{key}'")
    return text


def _overrides(entry: dict[str, Any], series_id: str, lag_days: int) -> tuple[ReleaseOverride, ...]:
    result: list[ReleaseOverride] = []
    seen: set[date] = set()
    for item in _entries(entry, "overrides"):
        where = f"override of {series_id}"
        obs_date = _date(item.get("obs_date"), f"{where}.obs_date")
        available_date = _date(item.get("available_date"), f"{where}.available_date")
        if obs_date in seen:
            raise ValueError(f"duplicated override of {series_id} for {obs_date.isoformat()}")
        seen.add(obs_date)
        if available_date <= obs_date + timedelta(days=lag_days):
            raise ValueError(
                f"override of {series_id} for {obs_date.isoformat()} must be later than "
                f"obs_date + lag_days ({lag_days}); overrides only delay a value"
            )
        result.append(
            ReleaseOverride(
                series_id=series_id,
                obs_date=obs_date,
                available_date=available_date,
                reason=_text(item, "reason", where),
                source=_text(item, "source", where),
            )
        )
    return tuple(sorted(result, key=lambda o: o.obs_date))


def load_macro_lags(
    path: Path | None = None, series_ids: Sequence[str] | None = None
) -> list[MacroLag]:
    """Load ``config/macro_lags.yaml`` (``series: {ID: {frequency, lag_days, source,
    overrides}}``), see docs/decisions/0005-macro-publication-lags.md.

    ``series_ids`` (default: the series of ``config/macro.yaml``) must match the configured
    series exactly, so no series reaches ``macro_asof`` without an explicit lag. Raises
    ValueError on a malformed file, a blocked series, a missing/extra series, or an override
    that is not later than ``obs_date + lag_days``.
    """
    raw = yaml.safe_load((path or MACRO_LAGS_PATH).read_text(encoding="utf-8"))
    section = raw.get("series") if isinstance(raw, dict) else None
    if not isinstance(section, dict) or not section:
        raise ValueError("macro lags file must hold a non-empty 'series' mapping")

    result: list[MacroLag] = []
    for key, entry in section.items():
        series_id = str(key).strip().upper()
        if series_id in BLOCKED_SERIES:
            raise ValueError(f"series {series_id} is copyrighted and must not be used")
        if any(lag.series_id == series_id for lag in result):
            raise ValueError(f"duplicated macro lag series: {series_id}")
        if not isinstance(entry, dict):
            raise ValueError(f"macro lag of {series_id} must be a mapping")
        frequency = entry.get("frequency")
        if frequency not in FREQUENCIES:
            raise ValueError(f"frequency of {series_id} must be one of {FREQUENCIES}")
        lag_days = _int(entry.get("lag_days"), f"lag_days of {series_id}", 0)
        result.append(
            MacroLag(
                series_id=series_id,
                frequency=frequency,
                lag_days=lag_days,
                source=_text(entry, "source", f"macro lag of {series_id}"),
                overrides=_overrides(entry, series_id, lag_days),
            )
        )

    expected = (
        {s.series_id for s in load_macro_series()}
        if series_ids is None
        else {s.upper() for s in series_ids}
    )
    configured = {lag.series_id for lag in result}
    if missing := sorted(expected - configured):
        raise ValueError(f"macro series without a publication lag: {', '.join(missing)}")
    if extra := sorted(configured - expected):
        raise ValueError(f"publication lag for unknown macro series: {', '.join(extra)}")
    return result


# fact_regimes names its columns after these series, so the config cannot swap them.
REGIME_SIGNALS: dict[str, str] = {
    "regime": "NFCI",
    "robustness": "STLFSI4",
    "curve_inverted": "T10Y2Y",
    "unemployment_rising": "UNRATE",
}


def _number(value: object, where: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(value):
        raise ValueError(f"{where} must be a finite number")
    return float(value)


def _signal(section: dict[str, Any], role: str, known: set[str]) -> str:
    signal = str(section.get("signal") or "").strip().upper()
    if signal in BLOCKED_SERIES:
        raise ValueError(f"series {signal} is copyrighted and must not be used")
    if signal not in known:
        raise ValueError(f"{role}.signal {signal or '(missing)'} is not in config/macro_lags.yaml")
    if signal != REGIME_SIGNALS[role]:
        raise ValueError(
            f"{role}.signal must be {REGIME_SIGNALS[role]} (fact_regimes columns are named "
            "after it; changing it needs a schema change and an ADR)"
        )
    return signal


def _level_rule(raw: dict[str, Any], role: str, known: set[str]) -> LevelRule:
    section = _section(raw, role)
    signal = _signal(section, role, known)
    tension_at = _number(section.get("tension_at"), f"{role}.tension_at")
    stress_at = _number(section.get("stress_at"), f"{role}.stress_at")
    if not stress_at > tension_at:
        raise ValueError(f"{role}.stress_at must be greater than {role}.tension_at")
    vintage = _section(_section(raw, "vintages"), signal)
    return LevelRule(
        signal=signal,
        tension_at=tension_at,
        stress_at=stress_at,
        first_vintage=_date(vintage.get("first_vintage"), f"vintages.{signal}.first_vintage"),
        vintage_source=_text(vintage, "source", f"vintages.{signal}"),
    )


def _sahm_rule(flags: dict[str, Any], known: set[str]) -> SahmRule:
    section = _section(flags, "unemployment_rising")
    where = "flags.unemployment_rising"
    ma_months = _int(section.get("ma_months"), f"{where}.ma_months", 1)
    lookback = _int(section.get("lookback_months"), f"{where}.lookback_months", 1)
    ma_min = _int(section.get("ma_min_obs"), f"{where}.ma_min_obs", 1)
    lookback_min = _int(section.get("lookback_min_obs"), f"{where}.lookback_min_obs", 1)
    if ma_min > ma_months or lookback_min > lookback:
        raise ValueError(f"{where}: a *_min_obs cannot exceed its window")
    return SahmRule(
        signal=_signal(section, "unemployment_rising", known),
        ma_months=ma_months,
        ma_min_obs=ma_min,
        lookback_months=lookback,
        lookback_min_obs=lookback_min,
        threshold_pp=_positive_number(section.get("threshold_pp"), f"{where}.threshold_pp"),
    )


def _known_events(report: dict[str, Any]) -> tuple[KnownEvent, ...]:
    events: list[KnownEvent] = []
    for entry in _entries(report, "known_events"):
        name = _text(entry, "name", "known event")
        start = _date(entry.get("start"), f"known event {name}.start")
        end = _date(entry.get("end"), f"known event {name}.end")
        if end < start:
            raise ValueError(f"known event {name} ends before it starts")
        events.append(KnownEvent(name, start, end, _text(entry, "source", f"known event {name}")))
    return tuple(events)


def load_regime_rules(
    path: Path | None = None, series_ids: Sequence[str] | None = None
) -> RegimeRules:
    """Load ``config/regime_rules.yaml`` (see docs/decisions/0006-rules-based-regimes-v1.md).

    ``series_ids`` (default: the series of ``config/macro_lags.yaml``) are the signals that
    can be used. Raises ValueError on a malformed file, a blocked or unknown signal, a signal
    other than the one fact_regimes is named after, ``stress_at <= tension_at``, or a missing
    vintage.
    """
    raw = yaml.safe_load((path or REGIME_RULES_PATH).read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("regime rules file must be a mapping")
    known = (
        {lag.series_id for lag in load_macro_lags()}
        if series_ids is None
        else {s.upper() for s in series_ids}
    )
    flags = _section(raw, "flags")
    curve = _section(flags, "curve_inverted")
    report = _section(raw, "report")
    return RegimeRules(
        regime=_level_rule(raw, "regime", known),
        robustness=_level_rule(raw, "robustness", known),
        curve_signal=_signal(curve, "curve_inverted", known),
        curve_below=_number(curve.get("below"), "flags.curve_inverted.below"),
        sahm=_sahm_rule(flags, known),
        nfci_stale_days=_int(report.get("nfci_stale_days"), "report.nfci_stale_days", 1),
        known_events=_known_events(report),
    )


def default_portfolio_path() -> Path:
    """``config/portfolio.yaml`` if it exists, else ``config/portfolio.example.yaml``."""
    return PORTFOLIO_PATH if PORTFOLIO_PATH.exists() else PORTFOLIO_EXAMPLE_PATH


def load_portfolio(path: Path | None = None, universe: Sequence[str] | None = None) -> Portfolio:
    """Load a fixed-weight portfolio (``portfolio: {id, name, holdings: {ticker: weight}}``).

    ``universe`` defaults to the tickers of ``config/universe.yaml``. Raises ValueError on a
    malformed id, a ticker outside the universe, a duplicated ticker, a negative or
    non-numeric weight, or weights not summing to 1 (tolerance ``WEIGHT_SUM_TOLERANCE``).
    """
    raw = yaml.safe_load((path or default_portfolio_path()).read_text(encoding="utf-8"))
    section = raw.get("portfolio") if isinstance(raw, dict) else None
    if not isinstance(section, dict):
        raise ValueError("portfolio file must hold a 'portfolio' mapping")

    portfolio_id = str(section.get("id") or "").strip()
    if not _PORTFOLIO_ID.fullmatch(portfolio_id):
        raise ValueError("portfolio.id must use only lowercase letters, digits, '_' or '-'")
    name = str(section.get("name") or portfolio_id).strip()

    holdings = section.get("holdings")
    if not isinstance(holdings, dict) or not holdings:
        raise ValueError("portfolio.holdings must map ticker -> weight")
    allowed = set(universe) if universe is not None else {a.ticker for a in load_universe()}

    weights: dict[str, float] = {}
    for key, value in holdings.items():
        ticker = str(key).strip().upper()
        if ticker in weights:
            raise ValueError(f"duplicated ticker in portfolio: {ticker}")
        if ticker not in allowed:
            raise ValueError(f"portfolio ticker {ticker} is not in config/universe.yaml")
        numeric = isinstance(value, int | float) and not isinstance(value, bool)
        if not numeric or not math.isfinite(value):
            raise ValueError(f"weight of {ticker} must be a number")
        if value < 0:
            raise ValueError(f"weight of {ticker} must be >= 0 (long-only)")
        weights[ticker] = float(value)

    total = math.fsum(weights.values())
    if abs(total - 1.0) > WEIGHT_SUM_TOLERANCE:
        raise ValueError(f"portfolio weights must sum to 1.0, got {total:.12g}")
    return Portfolio(portfolio_id=portfolio_id, name=name, weights=weights)
