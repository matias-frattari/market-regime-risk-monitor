# ADR 0006: Rules-based market regimes v1

- **Status:** Accepted
- **Date:** 2026-11-25

## Context

Step 3.1 needs a transparent, reproducible label of the market regime on each trading day, to
report portfolio behaviour per regime later (Phase 4) and to compare against an unsupervised
model (v2). Inputs must be point-in-time: `macro_asof` already applies the publication lags of
ADR 0005. VIXCLS and BAMLH0A0HYM2 are excluded for licensing (ADR 0002).

## Decision

`mrm regimes` reads NFCI, STLFSI4, T10Y2Y and UNRATE from `macro_asof` (after
`prepare_clean_prices`) on the NYSE trading days of `prices_clean` and rebuilds `fact_regimes`,
one row per trading day. Rules and thresholds live in `config/regime_rules.yaml`.

| Output | Rule | Source of the threshold |
|---|---|---|
| `regime` | NFCI < 0 → `calm`; 0 ≤ NFCI < 1 → `tension`; NFCI ≥ 1 → `stress` | Definition of NFCI: mean 0 and standard deviation 1 over its history since 1971, positive = tighter than average (Chicago Fed). 0 = average, 1 = one standard deviation tighter. |
| `stlfsi4_regime` (robustness only) | Same thresholds on STLFSI4 | STLFSI4: 0 = normal stress level (St. Louis Fed). It never changes `regime`. The report gives the share of days with the same label. |
| `curve_inverted` | T10Y2Y < 0 | Definition of an inverted 10y–2y curve. |
| `unemployment_rising` | Sahm gap ≥ 0.50 pp. The gap is the 3-month average of UNRATE minus the minimum of the 3-month averages over the previous 12 months. | Sahm (2019) recession indicator, standard 0.50 threshold. |

- **Fixed a priori.** The thresholds were written before any result was computed. They come from
  the index definitions above, not from our data, and are never tuned on returns or backtests.
  Changing one requires a new ADR.
- **Flags are overlays.** They sit next to the regime and do not change it. SPY realized
  volatility is not used in v1. It can come back in v2.
- **No smoothing or hysteresis.** The regime is the label of the as-of NFCI value. Changes per
  year are reported as they come out.
- **Sahm on point-in-time data.** The monthly UNRATE series is rebuilt from the
  `(value_date, value)` pairs that `macro_asof` exposes, on a calendar-month grid:
  - A 3-month average needs 2 of its 3 months. The 12-month minimum needs 11 of its 12 averages.
  - A month that was never published (October 2025) stays NaN and is bridged by the 2-of-3 rule,
    so it creates no NULLs after the warm-up.
  - Each trading day takes the gap of its current reference month. Every month in that gap was
    published no later than the current one (`available_date` is non-decreasing).
  - The comparison uses a 1e-9 float tolerance, because 3-month averages of one-decimal rates
    are multiples of 1/30.
- **History label of NFCI and STLFSI4** (`nfci_history`, `stlfsi4_history`). The label is
  `backcast` when the value's `available_date` is before the index's first ALFRED vintage
  (NFCI 2011-06-02, STLFSI4 2022-11-17, per ADR 0005), and `revised` otherwise. The same
  criterion is used in the code, the schema comment and this ADR. **Neither label is a real-time
  value.** `backcast` values did not exist when they would have been used. `revised` values were
  published in real time, but `fact_regimes` holds FRED's latest, re-estimated vintage.
- **Columns are named after the series** (`nfci_value`, `stlfsi4_regime`, …). The config loader
  therefore rejects a different signal: swapping one needs a schema change and an ADR.
- **Report** (`mrm regimes`, aggregates only):
  - share of days per regime and per flag, NULLs included
  - regime changes per year
  - NFCI vs STLFSI4 agreement: on all days, and on days with real-time STLFSI4
    (`available_date ≥ 2022-11-17`), plus a confusion table
  - each NULL column with its date range and reason
  - runs of trading days with `nfci_age_days > 14`, each labelled with any known event it
    overlaps (the 2025 federal shutdown is listed in the config)

## Expected NULLs

- `sahm_gap` and `unemployment_rising` are **NULL from 2007-01-11 to 2007-12-12** (233 trading
  days), during the Sahm warm-up. `macro_asof` starts with the price calendar (2007-01-11), so
  on the first day only the latest UNRATE month (2006-11) is visible, and the rule needs about
  12 more months. The first gap is for reference month 2007-11 and is usable from 2007-12-13. Its
  first values use the shortened windows allowed by `ma_min_obs` / `lookback_min_obs`. **So the
  flag does not cover the start of the 2007-08 crisis.** Financial stress started in August 2007
  and the NBER business-cycle peak is December 2007. Planning had estimated about Feb-2008. The
  exact end of the warm-up is 2007-12-12, as printed by `mrm regimes`.
- `regime` and the `nfci_*` columns would be NULL only before NFCI has an as-of value. That is 0
  days in the current warehouse. The report counts and explains any NULL, and labels NULLs it
  cannot explain as `unexpected`.

## Expected distribution

NFCI is centred on its 1971–today average, and conditions since 2010 were mostly looser than
that average. By construction, then, **most days are expected to be `calm`**, with `tension`
and `stress` concentrated in 2007–09 and short episodes. If the output shows this, it is
reported as is and `config/regime_rules.yaml` is not touched. A skewed distribution is a
property of the definition, not a reason to retune.

First run (2026-11-25, 5,000 trading days 2007-01-11..2026-11-24):
- regime: calm 87.82%, tension 8.62%, stress 3.56%, NULL 0. There are 6 regime changes in total
  (2007: 1, 2008: 1, 2009: 2, 2020: 2). `stress` falls only in 2008–09 and `tension` only in
  2007–09 and 2020. Every year 2010–2019 and 2021–2026 is entirely `calm`, including the 2022
  rate shock and the March 2023 bank failures.
- `curve_inverted`: true on 12.24% of days. `unemployment_rising`: true on 18.06%, NULL 233.
- NFCI history: backcast 22.14%, revised 77.86%.
- NFCI vs STLFSI4 agreement: 73.68% on all days, and 96.82% on the 1,007 days with real-time
  STLFSI4. Every disagreement has STLFSI4 one or two levels above NFCI (for example, 964 days
  NFCI `calm` / STLFSI4 `tension`). The same thresholds mean different things on the two indices
  (STLFSI4's 2007+ standard deviation is 1.21 vs 0.58 for NFCI, ADR 0005), so agreement is a
  robustness indicator, not a validation. Disagreements span most years (most in 2016 with 218
  days, then 2008, 2010–2012, 2015, 2020 and 2022): STLFSI4 flags `tension` in episodes that
  NFCI keeps `calm`.
- Stale NFCI (age > 14 days): there are 3 runs: 2012-11-05..07, 2020-01-06..08 and
  2022-12-27..2023-01-05 (max age 27 d). These are the skipped releases listed as overrides in
  ADR 0005. **None overlaps the 2025 federal shutdown**: NFCI kept being published.

## Limitations

1. **Revisions (look-ahead not removed).** ALFRED vintages are out of scope for v1. NFCI
   revisions are large next to the thresholds: the mean absolute revision is 0.147 and the max
   0.54, against a standard deviation of 0.58 since 2007 (ADR 0005). A day labelled `tension`
   today may have been `calm` in real time, and vice versa. The same applies to STLFSI4 and, to a
   lesser extent, to UNRATE (mean revision 0.05 pp) and the Sahm flag.
2. **Reconstructed history.** NFCI before its 2011-06-02 launch (`nfci_history = 'backcast'`)
   and STLFSI4 before 2022-11-17 are reconstructions. This covers the whole 2008 crisis, which is
   where almost all `stress` days fall.
3. **Conservative lags.** Values are used from the trading day after `available_date` (ADR 0005).
   Regimes therefore switch up to about one week after the reference Friday, and the Sahm flag
   about six weeks after its reference month.
4. **Single-index regime.** One index and two thresholds give few regime changes (6 since 2007).
   v1 is a transparent baseline, not a model of the cycle. The flags add context but are not
   combined into the label.
5. **No SPY volatility.** Market-implied or realized stress is not in v1. It is deferred to v2
   (GMM/HMM), where the disagreements with v1 will be reported.

## Alternatives considered

- **ALFRED vintages** (real-time NFCI/UNRATE). They would remove limitation 1, but not 2. They are
  out of scope for v1, as decided for step 3.1, and remain the main candidate improvement
  (ADR 0005).
- **Hysteresis / minimum duration.** This would reduce flip-flopping. It was not needed, given 6
  changes in 20 years, and adding it after seeing results would be tuning.
- **Simple 12-month change in UNRATE.** It is noisier and has no threshold from the literature,
  so the Sahm rule was chosen instead.
