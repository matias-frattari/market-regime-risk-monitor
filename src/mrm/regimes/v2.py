"""HMM regimes v2 (ADR 0007): point-in-time and full-sample variants. Pure, no database.

* ``point_in_time`` (official): expanding z-scores; first fit ``R_0`` on the first trading day
  with ``min_train_days`` valid rows before it, refits every ``refit_every_days`` trading days.
  Fit ``k`` trains on valid rows ``< R_k`` and labels ``[R_k, R_{k+1})`` with **filtered**
  probabilities. Row ``t`` depends only on rows ``<= t``.
* ``full_sample`` (descriptive, ``is_point_in_time = false``): one fit on every valid row,
  full-sample z-scores, **smoothed** posteriors. Uses the future: never for performance
  comparisons or backtests.

A row is *valid* when all three z-scores are known. A row of the point-in-time variant after
``R_0`` that is not valid gets a prediction-only filter step (no emission) and is counted.
"""

from dataclasses import dataclass
from datetime import date

import numpy as np
import pandas as pd

from mrm.regimes.features import FEATURES, VOL_FEATURE, expanding_zscore, full_sample_zscore
from mrm.regimes.hmm import FitResult, fit_best, forward_filter, smoothed
from mrm.regimes.rules import REGIME_LABELS
from mrm.storage.models import RegimeModelConfig

POINT_IN_TIME = "point_in_time"
FULL_SAMPLE = "full_sample"
VARIANTS: tuple[str, ...] = (POINT_IN_TIME, FULL_SAMPLE)
PROB_COLUMNS: tuple[str, ...] = tuple(f"p_{label}" for label in REGIME_LABELS)
RAW_COLUMNS: dict[str, str] = {
    "spy_vol_60d": "spy_vol_60d",
    "nfci": "nfci_value",
    "t10y2y": "t10y2y_value",
}
COLUMNS_V2: tuple[str, ...] = (
    "trade_date", "variant", "is_point_in_time", "regime", *PROB_COLUMNS,
    "spy_vol_60d", "nfci_value", "t10y2y_value", "z_spy_vol", "z_nfci", "z_t10y2y",
    "train_end_date", "train_rows",
)  # fmt: skip


@dataclass(frozen=True, slots=True)
class Refit:
    """One model fit: where it applies and how it went (for the report)."""

    variant: str
    first_day: date  # first trading day labelled with this fit
    last_day: date
    train_end_date: date
    train_rows: int
    log_likelihood: float
    n_iter: int
    converged: bool
    seed: int
    vol_means: tuple[float, ...]  # emission mean of z_spy_vol per ordered state


@dataclass(frozen=True, slots=True)
class RegimesV2:
    """Long frame (``COLUMNS_V2``, one row per trading day per variant), refit log and the
    number of point-in-time rows after ``R_0`` that had a missing feature."""

    frame: pd.DataFrame
    refits: tuple[Refit, ...]
    prediction_only_rows: int


def refit_positions(valid: np.ndarray, min_train: int, every: int) -> list[int]:
    """Grid positions ``R_k`` of the point-in-time fits.

    ``R_0`` = first position with at least ``min_train`` valid rows strictly before it;
    ``R_k = R_0 + k * every``. Depends only on rows before each position, so truncating the
    grid never moves an earlier refit.
    """
    before = np.concatenate([[0], np.cumsum(valid)[:-1]])
    eligible = np.flatnonzero(before >= min_train)
    return list(range(int(eligible[0]), len(valid), every)) if eligible.size else []


def _fit(train: np.ndarray, cfg: RegimeModelConfig) -> FitResult:
    return fit_best(
        train,
        n_states=cfg.n_states,
        n_init=cfg.n_init,
        seed=cfg.seed,
        max_iter=cfg.max_iter,
        tol=cfg.tol,
        order_column=FEATURES.index(VOL_FEATURE),
    )


def _refit(
    variant: str,
    fit: FitResult,
    index: pd.DatetimeIndex,
    first: int,
    last: int,
    train_end: int,
    train_rows: int,
) -> Refit:
    vol = FEATURES.index(VOL_FEATURE)
    return Refit(
        variant=variant,
        first_day=index[first].date(),
        last_day=index[last].date(),
        train_end_date=index[train_end].date(),
        train_rows=train_rows,
        log_likelihood=fit.log_likelihood,
        n_iter=fit.n_iter,
        converged=fit.converged,
        seed=fit.seed,
        vol_means=tuple(float(m) for m in fit.params.means[:, vol]),
    )


def _frame(
    variant: str,
    features: pd.DataFrame,
    z: pd.DataFrame,
    probs: np.ndarray,
    train_end: list[date | None],
    train_rows: np.ndarray,
) -> pd.DataFrame:
    labelled = ~np.isnan(probs).any(axis=1)
    regime = np.full(len(probs), None, dtype=object)
    regime[labelled] = np.array(REGIME_LABELS, dtype=object)[probs[labelled].argmax(axis=1)]
    frame = pd.DataFrame(
        {
            "trade_date": features.index,
            "variant": variant,
            "is_point_in_time": variant == POINT_IN_TIME,
            "regime": regime,
            **{c: probs[:, i] for i, c in enumerate(PROB_COLUMNS)},
            **{RAW_COLUMNS[c]: features[c].to_numpy() for c in FEATURES},
            **{c: z[c].to_numpy() for c in z.columns},
            "train_end_date": pd.to_datetime(pd.Series(train_end, dtype=object)),
            "train_rows": pd.array(train_rows, dtype="Int64"),
        }
    )
    return frame[list(COLUMNS_V2)]


def build_point_in_time(
    features: pd.DataFrame, cfg: RegimeModelConfig
) -> tuple[pd.DataFrame, list[Refit], int]:
    """Official variant: frame, refit log, prediction-only rows after ``R_0``."""
    z = expanding_zscore(features, cfg.zscore_min_obs)
    x = z.to_numpy()
    valid = ~np.isnan(x).any(axis=1)
    n = len(x)
    probs = np.full((n, cfg.n_states), np.nan)
    train_end: list[date | None] = [None] * n
    train_rows = np.full(n, pd.NA, dtype=object)
    refits: list[Refit] = []
    positions = refit_positions(valid, cfg.min_train_days, cfg.refit_every_days)
    if positions:
        start = int(np.argmax(valid))  # the filter starts on the first valid row
        for k, r in enumerate(positions):
            end = positions[k + 1] if k + 1 < len(positions) else n
            train_mask = valid[:r]
            fit = _fit(x[:r][train_mask], cfg)
            filtered, _ = forward_filter(x[start:end], fit.params)
            probs[r:end] = filtered[r - start :]
            last_train = int(np.flatnonzero(train_mask)[-1])
            rows = int(train_mask.sum())
            train_end[r:end] = [features.index[last_train].date()] * (end - r)
            train_rows[r:end] = rows
            refits.append(_refit(POINT_IN_TIME, fit, features.index, r, end - 1, last_train, rows))
    prediction_only = int((~valid[positions[0] :]).sum()) if positions else 0
    frame = _frame(POINT_IN_TIME, features, z, probs, train_end, train_rows)
    return frame, refits, prediction_only


def build_full_sample(
    features: pd.DataFrame, cfg: RegimeModelConfig
) -> tuple[pd.DataFrame, list[Refit]]:
    """Descriptive variant (NOT point-in-time): one fit, smoothed posteriors."""
    z = full_sample_zscore(features)
    x = z.to_numpy()
    valid = ~np.isnan(x).any(axis=1)
    n = len(x)
    probs = np.full((n, cfg.n_states), np.nan)
    train_end: list[date | None] = [None] * n
    train_rows = np.full(n, pd.NA, dtype=object)
    refits: list[Refit] = []
    if valid.sum() >= 2:
        fit = _fit(x[valid], cfg)
        probs[valid] = smoothed(fit, x[valid])
        positions = np.flatnonzero(valid)
        rows = int(valid.sum())
        last = features.index[positions[-1]].date()
        for p in positions:
            train_end[p] = last
            train_rows[p] = rows
        first, final = int(positions[0]), int(positions[-1])
        refits.append(_refit(FULL_SAMPLE, fit, features.index, first, final, final, rows))
    return _frame(FULL_SAMPLE, features, z, probs, train_end, train_rows), refits


def build_regimes_v2(features: pd.DataFrame, cfg: RegimeModelConfig) -> RegimesV2:
    """Both variants stacked (point-in-time first), refit log and prediction-only count."""
    pit, pit_refits, prediction_only = build_point_in_time(features, cfg)
    full, full_refits = build_full_sample(features, cfg)
    frame = pd.concat([pit, full], ignore_index=True)
    return RegimesV2(frame, tuple(pit_refits + full_refits), prediction_only)
