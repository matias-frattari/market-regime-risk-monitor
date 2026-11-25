"""Aggregate report of ``fact_regimes`` (pure; prints counts, shares and dates only).

Never prints prices, returns or index values: only regime/flag shares, counts of changes,
agreement rates, NULL counts and date ranges.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date

import pandas as pd

from mrm.regimes.rules import REGIME_LABELS
from mrm.storage.models import KnownEvent, RegimeRules

FLAGS: tuple[str, ...] = ("curve_inverted", "unemployment_rising")
NULL_REASONS: dict[str, str] = {
    "regime": "NFCI has no as-of value yet",
    "nfci_": "NFCI has no as-of value yet",
    "stlfsi4_": "STLFSI4 has no as-of value yet",
    "regime_agrees": "regime or stlfsi4_regime is NULL",
    "t10y2y_": "T10Y2Y has no as-of value yet",
    "curve_inverted": "T10Y2Y has no as-of value yet",
    "unrate_": "UNRATE has no as-of value yet",
    "sahm_gap": "Sahm warm-up: not enough UNRATE months visible in macro_asof yet",
    "unemployment_rising": "Sahm warm-up: not enough UNRATE months visible in macro_asof yet",
}


@dataclass(frozen=True, slots=True)
class StaleRun:
    """Consecutive trading days whose NFCI as-of value is older than the threshold."""

    start: date
    end: date
    n_days: int
    max_age_days: int
    value_dates: tuple[date, ...]
    events: tuple[str, ...]


def _day(value: object) -> date:
    return pd.Timestamp(value).date()


def _pct(n: int, total: int) -> str:
    return f"{100.0 * n / total:6.2f}%" if total else "     -"


def _bool_counts(values: pd.Series) -> tuple[int, int, int]:
    flags = values.astype("boolean")
    known = flags.notna()
    true = int(flags[known].astype(bool).sum())
    return true, int(known.sum()) - true, int((~known).sum())


def label_counts(values: pd.Series, labels: Sequence[object]) -> list[tuple[str, int]]:
    """(label, days) for each label plus NULL."""
    counts = [(str(label), int((values == label).sum())) for label in labels]
    return [*counts, ("NULL", int(values.isna().sum()))]


def changes_per_year(regimes: pd.DataFrame) -> dict[int, int]:
    """Days on which the regime differs from the previous non-NULL regime, per calendar year.

    Every year in the data appears (0 when nothing changed); the first labelled day is not
    a change.
    """
    labelled = regimes.dropna(subset=["regime"])
    years = pd.to_datetime(regimes["trade_date"]).dt.year
    result = {int(y): 0 for y in sorted(years.unique())}
    changed = labelled["regime"].ne(labelled["regime"].shift()) & labelled["regime"].shift().notna()
    for year in pd.to_datetime(labelled.loc[changed, "trade_date"]).dt.year:
        result[int(year)] += 1
    return result


def agreement(regimes: pd.DataFrame, mask: pd.Series | None = None) -> tuple[int, int]:
    """(days where NFCI and STLFSI4 regimes agree, days where both are labelled)."""
    frame = regimes if mask is None else regimes[mask]
    both = frame["regime"].notna() & frame["stlfsi4_regime"].notna()
    agree = frame.loc[both, "regime"] == frame.loc[both, "stlfsi4_regime"]
    return int(agree.sum()), int(both.sum())


def null_summary(regimes: pd.DataFrame) -> list[tuple[str, int, date, date, str]]:
    """(column, NULL days, first, last, reason) for each column with NULLs."""
    rows = []
    for column in regimes.columns:
        nulls = regimes[column].isna()
        if not nulls.any():
            continue
        days = regimes.loc[nulls, "trade_date"]
        reason = next(
            (text for key, text in NULL_REASONS.items() if column == key or (
                key.endswith("_") and column.startswith(key))),
            "unexpected",
        )  # fmt: skip
        rows.append((column, int(nulls.sum()), _day(days.min()), _day(days.max()), reason))
    return rows


def nulls_after_warmup(regimes: pd.DataFrame, column: str) -> int:
    """NULLs after the first non-NULL value of ``column`` (expected 0: warm-up only)."""
    known = regimes[column].notna().to_numpy()
    if not known.any():
        return 0
    return int((~known[known.argmax() :]).sum())


def stale_runs(
    regimes: pd.DataFrame, threshold_days: int, events: Sequence[KnownEvent] = ()
) -> list[StaleRun]:
    """Runs of consecutive trading days with ``nfci_age_days > threshold_days``, each with the
    names of the known events whose [start, end] window overlaps the run."""
    stale = (regimes["nfci_age_days"].astype("Float64") > threshold_days).fillna(False)
    stale = stale.astype(bool).to_numpy()
    runs: list[StaleRun] = []
    i = 0
    while i < len(stale):
        if not stale[i]:
            i += 1
            continue
        j = i
        while j + 1 < len(stale) and stale[j + 1]:
            j += 1
        part = regimes.iloc[i : j + 1]
        start, end = _day(part["trade_date"].iloc[0]), _day(part["trade_date"].iloc[-1])
        runs.append(
            StaleRun(
                start=start,
                end=end,
                n_days=j - i + 1,
                max_age_days=int(part["nfci_age_days"].max()),
                value_dates=tuple(sorted({_day(v) for v in part["nfci_value_date"]})),
                events=tuple(e.name for e in events if e.start <= end and start <= e.end),
            )
        )
        i = j + 1
    return runs


def format_report(regimes: pd.DataFrame, rules: RegimeRules) -> list[str]:
    """Lines of the ``mrm regimes`` report."""
    n = len(regimes)
    r, s = rules.regime, rules.robustness
    lines = [
        f"rules: {r.signal} calm < {r.tension_at:g} <= tension < {r.stress_at:g} <= stress; "
        f"robustness {s.signal} same levels ({s.tension_at:g}, {s.stress_at:g}); "
        f"curve_inverted {rules.curve_signal} < {rules.curve_below:g}; "
        f"unemployment_rising Sahm >= {rules.sahm.threshold_pp:.2f} pp",
        "",
        f"{'regime (' + r.signal + ')':<24}{'days':>8}{'share':>9}",
    ]
    counts = label_counts(regimes["regime"], REGIME_LABELS)
    lines += [f"  {k:<22}{v:>8}{_pct(v, n):>9}" for k, v in counts]
    lines += ["", f"{'flag':<24}{'true':>8}{'share':>9}{'false':>8}{'share':>9}{'NULL':>8}"]
    for flag in FLAGS:
        t, f, z = _bool_counts(regimes[flag])
        lines.append(f"  {flag:<22}{t:>8}{_pct(t, n):>9}{f:>8}{_pct(f, n):>9}{z:>8}")

    lines += ["", f"{r.signal} history (all values are FRED's latest vintage: re-estimated, "
              "not real time)"]  # fmt: skip
    for label, count in label_counts(regimes["nfci_history"], ("backcast", "revised")):
        note = {
            "backcast": f"available_date < first vintage {r.first_vintage}",
            "revised": f"available_date >= {r.first_vintage}",
        }.get(label, "")
        lines.append(f"  {label:<22}{count:>8}{_pct(count, n):>9}  {note}")

    changes = changes_per_year(regimes)
    lines += ["", f"regime changes per year (total {sum(changes.values())})"]
    lines += [f"  {year}: {count}" for year, count in changes.items()]

    lines += ["", f"{r.signal} vs {s.signal} agreement (same regime label)"]
    real_time = regimes["stlfsi4_history"] == "revised"
    for name, mask in (
        ("all days", None),
        (f"{s.signal} available_date >= {s.first_vintage}", real_time),
    ):
        agree, both = agreement(regimes, mask)
        lines.append(f"  {name:<44}{agree:>6} / {both:<6}{_pct(agree, both):>9}")
    labelled = regimes.dropna(subset=["regime", "stlfsi4_regime"])
    table = pd.crosstab(labelled["regime"], labelled["stlfsi4_regime"]).reindex(
        index=list(REGIME_LABELS), columns=list(REGIME_LABELS), fill_value=0
    )
    lines.append(f"  days by {r.signal} (rows) x {s.signal} (columns): "
                 + "".join(f"{c:>9}" for c in REGIME_LABELS))  # fmt: skip
    for label in REGIME_LABELS:
        lines.append(f"  {'':<34}{label:<10}" + "".join(f"{v:>9}" for v in table.loc[label]))

    lines += ["", "NULL values (column, days, first .. last, reason)"]
    nulls = null_summary(regimes)
    if not nulls:
        lines.append("  none")
    for column, count, first, last, reason in nulls:
        lines.append(f"  {column:<22}{count:>6}  {first} .. {last}  {reason}")
    lines.append(
        f"  sahm_gap NULL after its first value (expected 0): "
        f"{nulls_after_warmup(regimes, 'sahm_gap')}"
    )

    runs = stale_runs(regimes, rules.nfci_stale_days, rules.known_events)
    events = ", ".join(f"{e.name} {e.start}..{e.end}" for e in rules.known_events) or "none"
    lines += ["", f"stale {r.signal} (nfci_age_days > {rules.nfci_stale_days}): {len(runs)} "
              f"run(s); known events: {events}"]  # fmt: skip
    for run in runs:
        overlap = ", ".join(run.events) if run.events else "no known event"
        value_dates = ", ".join(str(d) for d in run.value_dates)
        lines.append(
            f"  {run.start} .. {run.end}  {run.n_days:>3} days  max age {run.max_age_days} d  "
            f"value_date {value_dates}  overlaps: {overlap}"
        )
    return lines
