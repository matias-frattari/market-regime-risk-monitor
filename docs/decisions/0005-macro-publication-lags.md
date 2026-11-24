# ADR 0005: Publication lags of the monthly and weekly macro series

- **Status:** Accepted
- **Date:** 2026-11-24

## Context

`raw_macro.obs_date` is FRED's **reference** date, not the date a value became public. Phase
1.6 exposed only the daily Treasury series (DGS10, DGS2, DGS3MO, T10Y2Y) in `macro_asof`, with
the rule `obs_date < trade_date`. CPIAUCSL, UNRATE, FEDFUNDS, NFCI and STLFSI4 were left out:
joining them on `obs_date` would use, for example, the October unemployment rate on October 1,
about five weeks before it was published. The regimes of Phase 3 need them without look-ahead.

## Decision

Each series gets a publication lag in `config/macro_lags.yaml`, synced into
`macro_publication_lags` (plus `macro_release_overrides`) by `prepare_clean_prices`:

- `available_date = obs_date + lag_days`, or the override's date when one is listed.
- `macro_asof` uses a value only on trading days **strictly after** `available_date`
  (`available_date < trade_date`). The release day itself is excluded, which is conservative by
  up to one day (CPI and the jobs report come out at 8:30 ET, before the open).
- `lag_days` covers every first release observed on ALFRED since 2005 except releases delayed
  by extraordinary events, which are listed one by one as overrides with their ALFRED date. An
  override must be later than `obs_date + lag_days`: overrides only delay a value.
- The daily series keep `lag_days = 0`, which is exactly the Phase 1.6 rule.
- Every series of `config/macro.yaml` must have an entry (no default lag), so a new series
  cannot reach `macro_asof` without an explicit decision.

| Series | Frequency | `obs_date` | Release (source) | First-release lag on ALFRED, days | `lag_days` | Overrides |
|---|---|---|---|---|---|---|
| CPIAUCSL | monthly, SA | 1st of the reference month | BLS CPI, usually mid following month, 8:30 ET | 37–59 | **56** | 1 |
| UNRATE | monthly, SA | 1st of the reference month | BLS Employment Situation, usually the first Friday, 8:30 ET | 31–80 | **41** | 3 |
| FEDFUNDS | monthly, NSA (average of the daily EFFR) | 1st of the reference month | FRED, first business days of the following month | 28–44 | **44** | 0 |
| NFCI | weekly, ending Friday | Friday ending the reference week | Chicago Fed: Wednesday 8:30 ET, Thursday when a federal holiday falls on or before Wednesday | 5–6 on 803 of 807 releases; 12, 12, 13, 20 | **6** | 4 |
| STLFSI4 | weekly, ending Friday | Friday ending the reference week | St. Louis Fed, weekly; no fixed weekday found in its documentation | 5–6 on all 209 releases (since 2022-11-17) | **6** | 0 |
| DGS10, DGS2, DGS3MO, T10Y2Y | daily | trading day | next business day | — | **0** | 0 |

Why these values:
- **CPIAUCSL 56:** the largest scheduled lag is the January 2015 CPI, released 2015-02-26 (56 d).
  The only longer one, 59 d for September 2013, was the 2013 shutdown and is an override. The
  September 2025 CPI (53 d, released 2025-10-24 during the 2025 shutdown) is already covered.
- **UNRATE 41:** the largest lag outside the overrides is the January 2026 report (released
  2026-02-11, 41 d; cause not verified). Regular January reports come out around the 10th (40 d).
- **FEDFUNDS 44:** lags vary with FRED's update timing and no event explains the long ones, so
  the maximum is used and there are no overrides.
- **NFCI / STLFSI4 6:** Friday + 6 covers the Wednesday and Thursday releases.

### Overrides (`config/macro_lags.yaml`)

The date of each override is its **first-release date on ALFRED** (source of every date below).
Causes marked *not verified* are plausible but were not confirmed with a source.

| Series | `obs_date` | `available_date` (ALFRED) | Cause |
|---|---|---|---|
| CPIAUCSL | 2013-09-01 | 2013-10-30 | October 2013 shutdown: release moved from 2013-10-16 (BLS revised schedule) |
| UNRATE | 2013-09-01 | 2013-10-22 | October 2013 shutdown (BLS revised schedule) |
| UNRATE | 2025-09-01 | 2025-11-20 | 2025-10-01..11-12 shutdown (BLS release of 2025-11-20) |
| UNRATE | 2025-11-01 | 2025-12-16 | Rescheduled after the 2025 shutdown (BLS release of 2025-11-20) |
| NFCI | 2012-10-26 | 2012-11-07 | Release skipped — *not verified* (week of Hurricane Sandy) |
| NFCI | 2019-12-27 | 2020-01-08 | Release skipped — *not verified* (New Year's Day week) |
| NFCI | 2022-12-16 | 2023-01-05 | Releases skipped — *not verified* (Christmas / New Year weeks) |
| NFCI | 2022-12-23 | 2023-01-05 | Releases skipped — *not verified* (Christmas / New Year weeks) |

Not overrides because they were never published: CPIAUCSL and UNRATE for October 2025 (`.` on
FRED; the household survey was not collected during the shutdown). `macro_asof` carries the
previous month and `age_days` shows the gap (CPIAUCSL reaches 116 days on 2025-12-26).

### Shape of `macro_asof`

- Selection: on trading day *t*, the observation with the **latest `obs_date`** among those with
  `available_date < t`. `available_date` is made non-decreasing in `obs_date` (running max), so
  a delayed older release delays the newer ones too (never earlier than their real release;
  with the current config no date changes). When several values become available on the same
  date (e.g. NFCI 2022-12-23 and 2022-12-30, both on 2023-01-05), the latest one wins.
- `is_filled` = `available_date < previous trading day`: the value was already available the
  previous trading day, i.e. it is not new today. For lag 0 this is the Phase 1.6 definition (a
  gap: FRED `.` or no row on the previous trading day). For monthly and weekly series it is
  `false` only on the first trading day a new release is usable.
- New columns `available_date` (audit of the rule) and `age_days = trade_date − value_date`
  (calendar days). `is_filled` cannot tell how stale a monthly value is; `age_days` can, e.g.
  for a release that was never published.

## Evidence

- **ALFRED initial releases** (`fred/series/observations`, `output_type=4`,
  `realtime_start=1776-07-04`, observations from 2005-01-01), queried once on 2026-11-24 with a
  throwaway script that is not committed. `realtime_start` of the initial release is the date
  the value first appeared on FRED/ALFRED, which is never before the source published it.
  Releases before a series' first vintage are excluded (NFCI first vintage 2011-06-02,
  STLFSI4 2022-11-17).
- **FRED series metadata** (`fred/series`): CPIAUCSL, UNRATE, FEDFUNDS "Monthly"; NFCI and
  STLFSI4 "Weekly, Ending Friday".
- BLS revised release schedule after the 2013 shutdown (September 2013 CPI moved from
  2013-10-16 to 2013-10-30): <https://www.bls.gov/bls/updated_release_schedule.htm>
- BLS Employment Situation for September 2025, released 2025-11-20; no October 2025 release;
  November 2025 report scheduled for 2025-12-16:
  <https://www.bls.gov/news.release/archives/empsit_11202025.pdf>
- Chicago Fed NFCI release timing (Wednesday 8:30 ET; Thursday when a federal holiday falls on
  or before Wednesday): <https://www.chicagofed.org/research/data/nfci/current-data>
- **Not verified:** a documented fixed release weekday for STLFSI4 (its lag rests on ALFRED
  only), the cause of the four NFCI delays, and the cause of the 41-day January 2026 UNRATE.

## Limitations

1. **Revisions (look-ahead not removed).** FRED serves the latest revised vintage. The lag
   avoids using a value before its release date, not using its *revised* value. Measured on
   2007+ (first release on ALFRED vs. `raw_macro` today):

   | Series | Observations revised | Mean abs revision | Max abs revision | Std dev of the series |
   |---|---|---|---|---|
   | NFCI | 806 / 807 | 0.147 | 0.541 | 0.583 |
   | STLFSI4 | 207 / 209 | 0.035 | 0.445 | 1.206 |
   | CPIAUCSL | 227 / 237 | 0.19 index points | 0.72 | 36.8 |
   | UNRATE | 112 / 237 | 0.05 pp | 0.20 pp | 2.18 |
   | FEDFUNDS | 0 / 238 | 0 | 0 | 1.90 |

   The NFCI revisions are large relative to the series (mean 0.147 and max 0.54 against a
   standard deviation of 0.58). **Thresholds on the NFCI level (e.g. NFCI > 0) are therefore
   fragile**: a reading that crossed a threshold in today's vintage may not have crossed it
   in real time, and vice versa.
2. **Reconstructed history.** NFCI was launched in 2011 (first ALFRED vintage 2011-06-02) and
   STLFSI4 in 2022 (first vintage 2022-11-17). **NFCI before 2011-06 and STLFSI4 before 2022-11
   are reconstructions** computed after the fact with a methodology and data that did not exist
   at the time; no lag can make them point-in-time. This covers the whole 2008 crisis in the
   sample (prices start 2007-01-04).
3. **Future delays.** A future delayed release that is not added as an override is shown from
   `obs_date + lag_days` in the history (live runs are not affected: the value is not in
   `raw_macro` until it is published). New delays must be added to the config with their
   ALFRED date.
4. **Fixed lags.** Conservative lags leave values staler than they were in reality (CPI about
   12 days on average, FEDFUNDS about 12, UNRATE about 6).

## Alternative: ALFRED vintages (not implemented)

ALFRED serves every vintage of a series with the same API key and endpoint
(`realtime_start`/`realtime_end`, `output_type=4` for initial releases), about one request per
series. Storing vintages (a new `raw_macro_vintages` table) would give the exact release date of
every observation and the value known at each date, removing the revision look-ahead
(limitation 1) and limitations 3–4. It does not fix limitation 2: there are no vintages before
a series was launched. **The decision to use ALFRED vintages is revisited when planning step
3.1** (regimes); it is not part of this step.

## Consequences

- `macro_asof` holds all 9 series of `config/macro.yaml`; the 4 daily series are row-for-row
  identical to Phase 1.6 (non-regression tests on synthetic data and on the real warehouse).
- Two new config tables, `macro_publication_lags` and `macro_release_overrides`; two new
  columns in `macro_asof`, `available_date` and `age_days`. `raw_macro` is unchanged.
- An invalid `config/macro_lags.yaml` exits with code 2 in `mrm metrics` and `mrm check`.
