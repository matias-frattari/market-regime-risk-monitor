"""Aggregate report of the HMM regimes v2 and their comparison with v1 (pure).

Prints shares, counts, durations, agreement rates and date ranges only: never prices, returns
or daily index values. The dated windows are **descriptive**: they are not targets and nothing
here feeds back into the model (ADR 0007).
"""

from collections.abc import Sequence

import numpy as np
import pandas as pd

from mrm.regimes.report import _day, _pct, changes_per_year, nulls_after_warmup
from mrm.regimes.rules import REGIME_LABELS
from mrm.regimes.v2 import FULL_SAMPLE, POINT_IN_TIME, Refit
from mrm.storage.models import RegimeModelConfig

V1 = "v1"
MODELS: tuple[str, ...] = (V1, POINT_IN_TIME, FULL_SAMPLE)
SHORT: dict[str, str] = {V1: "v1 rules", POINT_IN_TIME: "v2 PIT", FULL_SAMPLE: "v2 full*"}


def _null_reasons(cfg: RegimeModelConfig, variant: str) -> dict[str, str]:
    model = (
        f"warm-up before the first fit (window {cfg.vol_window} + zscore_min_obs "
        f"{cfg.zscore_min_obs} - 1 + min_train_days {cfg.min_train_days})"
        if variant == POINT_IN_TIME
        else f"no {cfg.vol_window}-day SPY volatility yet"
    )
    z_spy = (
        f"no {cfg.vol_window}-day volatility, then zscore_min_obs {cfg.zscore_min_obs} warm-up"
        if variant == POINT_IN_TIME
        else f"no {cfg.vol_window}-day SPY volatility yet"
    )
    z_macro = (
        f"zscore_min_obs {cfg.zscore_min_obs} warm-up"
        if variant == POINT_IN_TIME
        else "macro value missing"
    )
    return {
        "regime": model,
        "p_calm": model,
        "p_tension": model,
        "p_stress": model,
        "train_end_date": model,
        "train_rows": model,
        "spy_vol_60d": f"no {cfg.vol_window}-day SPY volatility yet",
        "z_spy_vol": z_spy,
        "z_nfci": z_macro,
        "z_t10y2y": z_macro,
        "nfci_value": "NFCI has no as-of value yet",
        "t10y2y_value": "T10Y2Y has no as-of value yet",
    }


def labels_by_model(v2: pd.DataFrame, v1: pd.DataFrame) -> pd.DataFrame:
    """Wide frame indexed by trade_date: one regime column per model (``MODELS``)."""
    wide = {
        variant: v2[v2["variant"] == variant].set_index("trade_date")["regime"]
        for variant in (POINT_IN_TIME, FULL_SAMPLE)
    }
    wide[V1] = v1.set_index("trade_date")["regime"]
    frame = pd.DataFrame(wide)[list(MODELS)]
    frame.index = pd.DatetimeIndex(frame.index, name="trade_date")
    return frame.sort_index()


def episodes(labels: pd.Series) -> pd.DataFrame:
    """Runs of consecutive trading days with the same non-NULL label (a NULL ends a run):
    columns ``regime``, ``start``, ``end``, ``days``."""
    values = labels.to_numpy(dtype=object)
    rows = []
    i = 0
    while i < len(values):
        if values[i] is None or (isinstance(values[i], float) and np.isnan(values[i])):
            i += 1
            continue
        j = i
        while j + 1 < len(values) and values[j + 1] == values[i]:
            j += 1
        rows.append((values[i], labels.index[i], labels.index[j], j - i + 1))
        i = j + 1
    return pd.DataFrame(rows, columns=["regime", "start", "end", "days"])


def confusion(rows: pd.Series, cols: pd.Series) -> pd.DataFrame:
    """Days by label of ``rows`` x label of ``cols`` on days where both are labelled."""
    both = rows.notna() & cols.notna()
    return pd.crosstab(rows[both], cols[both]).reindex(
        index=list(REGIME_LABELS), columns=list(REGIME_LABELS), fill_value=0
    )


def agreement_by_year(a: pd.Series, b: pd.Series) -> dict[int, tuple[int, int]]:
    """Year -> (days with the same label, days where both are labelled)."""
    both = a.notna() & b.notna()
    same = (a == b) & both
    years = a.index.year
    return {
        int(y): (int(same[years == y].sum()), int(both[years == y].sum()))
        for y in sorted(set(years))
    }


def _share_block(labels: pd.DataFrame, title: str) -> list[str]:
    lines = [title, f"  {'regime':<10}" + "".join(f"{SHORT[m]:>20}" for m in MODELS)]
    totals = {m: int(labels[m].notna().sum()) for m in MODELS}
    for label in REGIME_LABELS:
        cells = []
        for m in MODELS:
            n = int((labels[m] == label).sum())
            cells.append(f"{n:>8} {_pct(n, totals[m]):>9}  ")
        lines.append(f"  {label:<10}" + "".join(f"{c:>20}" for c in cells))
    lines.append(f"  {'labelled':<10}" + "".join(f"{totals[m]:>8}{'':>12}" for m in MODELS))
    return lines


def _confusion_lines(name_rows: str, name_cols: str, table: pd.DataFrame) -> list[str]:
    total = int(table.to_numpy().sum())
    agree = int(np.trace(table.to_numpy()))
    lines = [
        f"  {name_rows} (rows) x {name_cols} (columns): agree {agree} / {total} "
        f"({_pct(agree, total).strip()})",
        f"  {'':<12}" + "".join(f"{c:>9}" for c in REGIME_LABELS),
    ]
    lines += [f"  {r:<12}" + "".join(f"{v:>9}" for v in table.loc[r]) for r in REGIME_LABELS]
    return lines


def format_report_v2(
    frame: pd.DataFrame,
    refits: Sequence[Refit],
    prediction_only_rows: int,
    v1: pd.DataFrame,
    cfg: RegimeModelConfig,
) -> list[str]:
    """Lines of the ``mrm regimes-v2`` report."""
    lines = [
        f"model: Gaussian HMM, {cfg.n_states} states, {cfg.covariance_type} covariance, "
        f"n_init {cfg.n_init} (seeds {cfg.seed}..{cfg.seed + cfg.n_init - 1}), "
        f"max_iter {cfg.max_iter}, tol {cfg.tol:g}",
        f"features: {cfg.vol_ticker} {cfg.vol_window}-day realised vol, "
        f"{', '.join(cfg.macro_features)} (macro_asof); expanding z-scores "
        f"(zscore_min_obs {cfg.zscore_min_obs})",
        f"point-in-time: min_train_days {cfg.min_train_days}, refit every "
        f"{cfg.refit_every_days} trading days, FILTERED probabilities; "
        "states ordered by mean SPY vol",
        "* v2 full = full-sample, smoothed, NOT point-in-time: descriptive only, never for "
        "performance or backtests",
    ]

    # --- NULLs -------------------------------------------------------------------------
    lines += ["", "NULL values (variant, column, days, first .. last, reason)"]
    for variant in (POINT_IN_TIME, FULL_SAMPLE):
        part = frame[frame["variant"] == variant].reset_index(drop=True)
        reasons = _null_reasons(cfg, variant)
        for column in part.columns:
            nulls = part[column].isna()
            if not nulls.any():
                continue
            days = part.loc[nulls, "trade_date"]
            leading = nulls_after_warmup(part, column) == 0
            reason = reasons.get(column, "unexpected") if leading else "unexpected (gaps)"
            lines.append(
                f"  {variant:<14}{column:<16}{int(nulls.sum()):>6}  "
                f"{_day(days.min())} .. {_day(days.max())}  {reason}"
            )
    pit = frame[frame["variant"] == POINT_IN_TIME]
    expected = cfg.point_in_time_warmup
    lines.append(
        f"  point_in_time regime NULL: {int(pit['regime'].isna().sum())}; expected without SPY "
        f"gaps: {cfg.vol_window} + {cfg.zscore_min_obs} - 1 + {cfg.min_train_days} = {expected}"
    )
    lines.append(
        f"  point_in_time rows after the first fit with a missing feature "
        f"(prediction-only filter step): {prediction_only_rows}"
    )

    # --- refits ------------------------------------------------------------------------
    lines += [
        "",
        "fits (variant, days labelled, train rows, train end, log-lik, EM iters, converged, "
        "seed, mean z_spy_vol per state calm/tension/stress)",
    ]
    for r in refits:
        means = "/".join(f"{m:+.2f}" for m in r.vol_means)
        lines.append(
            f"  {r.variant:<14}{r.first_day} .. {r.last_day}  {r.train_rows:>5}  "
            f"{r.train_end_date}  {r.log_likelihood:>10.1f}  {r.n_iter:>3}  "
            f"{'yes' if r.converged else 'NO':<3}  {r.seed:>2}  {means}"
        )

    # --- shares, durations, changes ---------------------------------------------------------
    labels = labels_by_model(frame, v1)
    pit_days = labels[POINT_IN_TIME].notna()
    lines += [""] + _share_block(labels, "share of days per regime (each model's labelled days)")
    if pit_days.any():
        first, last = labels.index[pit_days][0].date(), labels.index[pit_days][-1].date()
        lines += [""] + _share_block(
            labels[pit_days], f"share of days per regime on the v2 PIT days ({first} .. {last})"
        )

    lines += ["", "regime episodes (runs of identical labels): count / mean length in trading days"]
    lines.append(f"  {'regime':<10}" + "".join(f"{SHORT[m]:>20}" for m in MODELS))
    runs = {m: episodes(labels[m]) for m in MODELS}
    for label in (*REGIME_LABELS, "all"):
        cells = []
        for m in MODELS:
            e = runs[m] if label == "all" else runs[m][runs[m]["regime"] == label]
            mean = f"{e['days'].mean():.1f}" if len(e) else "-"
            cells.append(f"{len(e)} / {mean}")
        lines.append(f"  {label:<10}" + "".join(f"{c:>20}" for c in cells))

    changes = {
        m: changes_per_year(pd.DataFrame({"trade_date": labels.index, "regime": labels[m].values}))
        for m in MODELS
    }
    lines += ["", "regime changes per year"]
    lines.append(f"  {'year':<10}" + "".join(f"{SHORT[m]:>12}" for m in MODELS))
    for year in changes[V1]:
        lines.append(f"  {year:<10}" + "".join(f"{changes[m].get(year, 0):>12}" for m in MODELS))
    lines.append(f"  {'total':<10}" + "".join(f"{sum(changes[m].values()):>12}" for m in MODELS))
    refit_days = {pd.Timestamp(r.first_day) for r in refits if r.variant == POINT_IN_TIME}
    pit_labels = labels[POINT_IN_TIME]
    changed = pit_labels.ne(pit_labels.shift()) & pit_labels.notna() & pit_labels.shift().notna()
    on_refit = int(sum(1 for d in labels.index[changed] if d in refit_days))
    lines.append(
        f"  v2 PIT changes on a refit day (parameters changed, not only data): {on_refit} "
        f"of {int(changed.sum())}"
    )

    # --- comparison ------------------------------------------------------------------------
    lines += ["", "confusion matrices (trading days where both models have a label)"]
    lines += _confusion_lines("v1 rules", "v2 PIT", confusion(labels[V1], labels[POINT_IN_TIME]))
    lines += _confusion_lines("v1 rules", "v2 full*", confusion(labels[V1], labels[FULL_SAMPLE]))
    lines += _confusion_lines(
        "v2 PIT", "v2 full*", confusion(labels[POINT_IN_TIME], labels[FULL_SAMPLE])
    )

    pairs = (
        ("v1 vs v2 PIT", V1, POINT_IN_TIME),
        ("v1 vs v2 full*", V1, FULL_SAMPLE),
        ("v2 PIT vs full*", POINT_IN_TIME, FULL_SAMPLE),
    )
    by_year = {name: agreement_by_year(labels[a], labels[b]) for name, a, b in pairs}
    lines += ["", "agreement per year (same label / both labelled)"]
    lines.append(f"  {'year':<6}" + "".join(f"{name:>24}" for name, _, _ in pairs))
    for year in by_year[pairs[0][0]]:
        cells = []
        for name, _, _ in pairs:
            same, both = by_year[name][year]
            cells.append(f"{same}/{both} {_pct(same, both).strip()}" if both else "-")
        lines.append(f"  {year:<6}" + "".join(f"{c:>24}" for c in cells))

    vol = frame[frame["variant"] == POINT_IN_TIME].groupby("regime")["spy_vol_60d"].mean()
    lines += [
        "",
        "mean SPY 60-day vol by v2 PIT regime: "
        + ", ".join(f"{k} {vol.get(k, np.nan):.1%}" for k in REGIME_LABELS),
    ]

    # --- dated windows (descriptive only) ----------------------------------------------------
    lines += [
        "",
        "dated windows (descriptive only, not targets): trading days tension / stress / NULL",
    ]
    for w in cfg.windows:
        inside = labels[(labels.index.date >= w.start) & (labels.index.date <= w.end)]
        lines.append(f"  {w.name} {w.start} .. {w.end} ({len(inside)} trading days)")
        for m in MODELS:
            t = int((inside[m] == "tension").sum())
            s = int((inside[m] == "stress").sum())
            z = int(inside[m].isna().sum())
            note = "  (all in warm-up)" if len(inside) and z == len(inside) else ""
            lines.append(f"    {SHORT[m]:<10}{t:>6} / {s:>6} / {z:>6}{note}")
    lines += [f"  source {w.name}: {w.source}" for w in cfg.windows]
    return lines
