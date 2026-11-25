# Methodology

> Regime classification: TODO (Phase 3).

## Risk metrics (Phase 2)

Code: `src/mrm/metrics/` (pure functions in `returns.py`, `risk.py`, `covariance.py`; rows
assembled in `pipeline.py`; database I/O only in `io.py`). Run: `uv run mrm metrics`.
Inputs: `close` from `prices_clean` and DGS3MO from `macro_asof`, never `raw_*`.
Constants: 252 trading days per year, confidence 95%, rolling windows 60d (volatility) and
126d (correlation, Euler).

### Calendar, missing quotes and NaN

- The calendar is the union of `trade_date` values in `prices_clean`, **filtered to NYSE
  trading days** (`mrm.quality.calendar`, special closures from `config/data_quality.yaml`).
  Rows on other dates (a weekend or holiday row from the provider) are discarded and counted in
  the run summary; otherwise one stray row in any ticker would put a NaN in every other ticker's
  returns and void their rolling windows for 60/126 days. Missing NYSE days are **not** added
  (gaps are reported by `mrm check`). Prices are **not** forward-filled or interpolated.
- A return on day *t* needs a close on *t* and on the previous calendar row. If either is
  missing the return is NaN, so a return never spans a gap. Return-based metrics (volatility,
  VaR/CVaR, Sharpe, correlation, Euler) therefore do not see the price move over the gap; this
  **understates** risk (it is not conservative).
- **Level-based asset metrics** (drawdown, max drawdown, annualized return) are computed from
  the close level instead, so the move across a gap is kept (see Formulas).
- **Portfolio:** NaN on any day where a holding's return is NaN; weights are never renormalised.
- **Full-period metrics** drop NaN returns. **Rolling metrics** need a complete window
  (`min_periods = window`): a NaN anywhere in the window gives no value for that date.
- **Portfolio drawdown and annualized return** are still built from the portfolio's daily
  returns: a day without a return (the gap day *and* the day after it) is skipped, so the move
  across a holding's missing quote is lost. Limitation, not triggered by the current data.
- NaN results are not stored (see `docs/data_dictionary.md`).
- In the current warehouse all 13 tickers share the same 5000 trading days, so none of these
  rules is triggered today; each is covered by a test.

### Returns

- Simple: `r_t = P_t / P_{t−1} − 1`. Log: `ℓ_t = ln(P_t / P_{t−1})`.
- Risk metrics (volatility, VaR/CVaR, covariance, Euler) use **simple** returns because the
  portfolio return is linear in them, which makes the Euler identity exact.
- **Portfolio** (Phase 2): fixed weights from the portfolio yaml, rebalanced daily:
  `r_p,t = Σ_i w_i r_i,t`. Monthly rebalancing with costs is the backtest's job (Phase 3).

### Risk-free rate (DGS3MO)

- DGS3MO is an annual yield in percent. Daily rate: `rf_t = (1 + y_t / 100)^(1/252) − 1`, so
  252 daily rates compound back to the annual yield.
- **Convention (approximation):** FRED quotes DGS3MO on an *investment (bond-equivalent) basis*,
  not as an effective annual rate, and the formula above treats it as one. Each trading day
  accrues the same rate, so weekends and holidays earn no extra interest (a Friday→Monday return
  gets one day of rf). The difference from an exact money-market accrual is a few basis points per
  year, negligible next to the Sharpe ratio's estimation error.
- `y_t` is `macro_asof.value` on `trade_date = t`: the latest observation with
  `obs_date < t`, i.e. the yield known at the close of *t−1*, which is the rate for the
  return over *(t−1, t]*. No look-ahead.
- **NULL at the start of the series:** the excess return is NaN and the day is excluded from
  the Sharpe ratio only (volatility, VaR, etc. still use it). Never filled backwards or with 0.
  `n_obs` of `sharpe` counts the days actually used.
- **`is_filled = true`** (FRED holiday `.` or missing row): the carried value is the latest
  information available on that date and is used as is. The run prints how many days were
  NULL or filled (current warehouse: 0 NULL, 38 filled out of 4999 return days).

### Formulas

| Metric | Definition |
|---|---|
| Annualized volatility | `σ_ann = std(r, ddof=1) · √252` (≥ 2 returns) |
| Rolling volatility 60d | same, over the 60 returns ending at *t* |
| Annualized return (asset) | geometric, from the close level: `(P_last / P_first)^(252 / N) − 1`, `N` = return intervals between the first and last close (calendar rows spanned − 1, intervals across a gap included). Without gaps this equals `exp(mean(ln(1 + r)) · 252) − 1` |
| Annualized return (portfolio) | `exp(mean(ln(1 + r_p)) · 252) − 1` over the days with a portfolio return |
| Drawdown (asset) | `DD_t = P_t / max_{s≤t} P_s − 1` on every close except the first |
| Drawdown (portfolio) | `W_t = Π_{s≤t}(1 + r_s)`, `W_0 = 1`; `DD_t = W_t / max(1, max_{s≤t} W_s) − 1` |
| Max drawdown | `min_t DD_t` (negative; −0.25 = 25% peak-to-trough loss) |
| Sharpe | `mean(r − rf) / std(r − rf, ddof=1) · √252`; no value when std ≤ 1e−12 (constant series) |
| Historical VaR 95% | `q = Q_0.05(r)` with linear interpolation between order statistics (numpy `method="linear"`); `VaR = −q` |
| Historical CVaR 95% | `−mean(r \| r ≤ q)` |
| Parametric VaR 95% | Gaussian with sample `μ`, `σ` (ddof=1): `VaR = −(μ − z σ)`, `z = Φ⁻¹(0.95) ≈ 1.6449` |
| Parametric CVaR 95% | `−(μ − σ φ(z) / 0.05)` (`φ(z)/0.05 ≈ 2.0627`) |
| Rolling correlation 126d | Pearson correlation of each pair over the 126 returns ending at *t*; no value when either sample std ≤ 1e−12 |
| Euler risk contribution | `Σ` = sample covariance (ddof=1) of the holdings' last 126 returns; `σ_p = √(wᵀΣw)`; `RC_i = w_i (Σw)_i / σ_p`; reported `× √252`, and `RC_i / σ_p` as a share; no value when daily `σ_p` ≤ 1e−12 |

**Sharpe vs annualized return.** The Sharpe numerator is the *arithmetic* mean daily excess
return, while `ann_return` is *geometric* (≈ arithmetic − σ²/2 per year). An asset can therefore
show a positive Sharpe with an `ann_return` below the average risk-free rate: TLT in the current
warehouse has `ann_return` ≈ 1.5%, average DGS3MO ≈ 1.6% and Sharpe ≈ +0.07 (arithmetic mean ≈
2.6%). Both definitions are standard; they answer different questions (average daily reward per
unit of risk vs compounded growth).

VaR/CVaR are **one-day** losses reported as positive numbers, computed on the full history
(rolling VaR is planned for Phase 3, per regime). By Euler's theorem for the homogeneous
function `σ_p(w)`, `Σ_i RC_i = σ_p`: the annualized contributions sum to the portfolio's 126d
rolling volatility on each date, and the shares sum to 1.

### No look-ahead

Every rolling or expanding value at *t* uses only data dated ≤ *t* (trailing windows, running
peak). Invariance tests change the data after *t* and check that the value at *t* does not move,
for rolling volatility, rolling correlation, rolling Euler contributions and drawdown (price- and
return-based). The risk-free rate at *t* uses only observations dated before *t* (`macro_asof`);
a test checks that a DGS3MO observation dated on the last trading day changes no metric, while
one dated the day before does.

### Limitations

- **Normality:** parametric VaR/CVaR assume Gaussian returns and understate tail risk when
  returns are fat-tailed; the historical version is reported next to it for comparison.
- **Full-period VaR** mixes calm and stress periods; it is not a forecast for today.
- **Estimation error:** 60d/126d windows are noisy; correlations and Euler shares can swing.
- **Daily rebalancing** of the Phase 2 portfolio is an idealisation without costs.
- **Gaps:** returns over missing quotes are dropped, not spread (see above); asset drawdown and
  annualized return keep the move, the portfolio's do not.
- **Survivorship and data:** the universe is a fixed list of ETFs that exist today; prices
  are provider-adjusted (ADR 0001) with documented corrections (ADR 0003).


## Macro publication lags and vintages (ADR 0005)

### Publication lags

`raw_macro.obs_date` is a reference date (1st of the month for monthly series, the Friday
ending the week for weekly ones), not the release date. `macro_asof` applies a per-series lag
from `config/macro_lags.yaml`:

- `available_date = obs_date + lag_days`, or a documented override (delayed release, date from
  ALFRED). A value is used on trading day *t* only if `available_date < t`, so never on its
  release day (conservative by up to one day).
- Lags: CPIAUCSL 56, UNRATE 41, FEDFUNDS 44, NFCI 6, STLFSI4 6, daily Treasury series 0
  (= the rule `obs_date < t`). Each covers every first release on ALFRED since 2005 except
  the 8 overrides (2013 and 2025 shutdowns for CPI/UNRATE, 4 skipped NFCI weeks).
- On *t* the value is the latest reference period already available; `age_days =
  t − value_date` measures its staleness (it grows when a release is missing, e.g. October 2025
  CPI and UNRATE were never published). `is_filled` means "already available on the previous
  trading day": a gap for the daily series, simply "not a new release today" for the others.
- Cost of being conservative: on average values are about 12 days staler than in reality for
  CPIAUCSL and FEDFUNDS, 6 for UNRATE and under 1 for NFCI and STLFSI4.

### Vintages and revisions (limitation)

FRED serves only the **latest vintage** of each series. The lag removes the look-ahead of the
release *date*, not of *revisions*: a backtest reading a past date sees the revised value, not
the one known at the time. Measured on 2007+ (first release on ALFRED vs. today):

| Series | Revised obs | Mean abs revision | Max abs revision | Std dev |
|---|---|---|---|---|
| NFCI | 806 / 807 | 0.147 | 0.541 | 0.583 |
| STLFSI4 | 207 / 209 | 0.035 | 0.445 | 1.206 |
| CPIAUCSL (SA) | 227 / 237 | 0.19 index pts | 0.72 | 36.8 |
| UNRATE (SA) | 112 / 237 | 0.05 pp | 0.20 pp | 2.18 |
| FEDFUNDS | 0 / 238 | 0 | 0 | 1.90 |

- **NFCI revisions are large relative to the series** (mean 0.147, max 0.54, against a standard
  deviation of 0.58), so **thresholds on the NFCI level are fragile**: a crossing in today's
  vintage may not have happened in real time.
- **Reconstructed history:** NFCI before 2011-06 and STLFSI4 before 2022-11 did not exist in
  real time (first ALFRED vintages 2011-06-02 and 2022-11-17); those values are reconstructions
  computed later. No lag fixes this; it covers the 2008 crisis of the sample.
- **Future delays** not yet added as overrides are shown from `obs_date + lag_days` in the
  history until the config is updated.
- **Alternative, not implemented:** ALFRED vintages (same API key) would give exact release
  dates and real-time values, removing the revision look-ahead but not the reconstructed
  history. Step 3.1 kept it out of scope (ADR 0006). See
  `docs/decisions/0005-macro-publication-lags.md` and
  `docs/decisions/0002-macro-series-licensing.md`.


## Regimes v1, rules-based (ADR 0006)

Rules and thresholds: `config/regime_rules.yaml`. They were fixed before any result was seen and
come from the index definitions, never from returns or backtests. Inputs are the point-in-time
values of `macro_asof` on each NYSE trading day (`available_date < trade_date`, ADR 0005).

### Rules

| Output | Rule |
|---|---|
| `regime` | NFCI < 0 → `calm`; 0 ≤ NFCI < 1 → `tension`; NFCI ≥ 1 → `stress`. NFCI has mean 0 and standard deviation 1 since 1971, and positive means tighter than average |
| `stlfsi4_regime` | Same thresholds on STLFSI4 (0 = normal stress). Robustness check only: it never changes `regime` |
| `curve_inverted` | T10Y2Y < 0 |
| `unemployment_rising` | Sahm rule: `gap_m = MA3_m − min(MA3_{m−12..m−1}) ≥ 0.50` pp |

Sahm details:
- `MA3` is the mean of the last 3 calendar months of UNRATE and needs at least 2 of them. The
  minimum needs at least 11 of the 12 prior averages.
- The monthly series is rebuilt from the `(value_date, value)` pairs that `macro_asof` exposes,
  so `gap_m` uses only months published by the time month *m* is published.
- Each trading day takes the gap of its current reference month.
- October 2025 was never published: its month stays NaN and the 2-of-3 rule bridges it.

No smoothing or hysteresis. Flags are overlays and never change the regime. SPY realized
volatility is not used in v1.

### NFCI is re-estimated, not real time

`nfci_history` (and `stlfsi4_history`) is `backcast` when the value's `available_date` is before
the index's first ALFRED vintage (NFCI 2011-06-02, STLFSI4 2022-11-17), and `revised` otherwise.
**Both are FRED's latest vintage.** NFCI re-estimates its whole history every week, and its
revisions are large next to the thresholds: mean absolute revision 0.147 and max 0.54, against
a standard deviation of 0.58 since 2007. A label near 0 or 1 may differ from the one that would
have been computed in real time. ALFRED vintages would fix the revisions but not the backcast.
They are out of scope for v1.

### Lags

The regime switches on the trading day after the NFCI release (reference Friday + 6 days, ADR
0005), not on the reference Friday. The Sahm flag reacts about six weeks after its reference
month (lag 41, plus the 3-month average). The lags are conservative by up to one day.

### No look-ahead (tests)

`tests/test_regimes_lookahead.py` checks two things:
1. An NFCI spike changes the regime only from the first trading day after `obs_date + lag`, with
   the lag read from `config/macro_lags.yaml`.
2. For several cutoffs D, the rows with `trade_date ≤ D` are identical when every release
   available on or after D is dropped or altered.

`tests/test_regimes_real.py` checks the real warehouse (skipped in CI):
- one row per `prices_clean` day
- NULLs only in the Sahm warm-up
- inputs equal to `macro_asof`
- `nfci_available_date < trade_date`

### Limitations

- **Revisions and backcast:** see above. Every `stress` day (2008–09) is a backcast.
- **Mostly calm by construction:** 87.8% of days are `calm` in the first run, and every year
  2010–2019 and 2021–2026 is entirely `calm`. This is a property of the NFCI definition (0 = the
  1971+ average), reported as is, not a reason to retune.
- **Few changes:** 6 regime changes since 2007. A single-index rule is a transparent baseline,
  not a model of the cycle (v2: HMM, below).
- **STLFSI4 agreement** is 73.7% on all days and 96.8% when STLFSI4 is real time. In every
  disagreement STLFSI4 is the higher level. The same thresholds mean different things on
  the two indices, so agreement is a robustness indicator, not a validation.
- **Sahm warm-up:** `sahm_gap` is NULL until 2007-12-12, so the flag misses the start of the
  2007-08 crisis (stress from August 2007, NBER peak December 2007).


## Regimes v2, Gaussian HMM (ADR 0007)

Hyperparameters: `config/regime_model.yaml`, fixed before the model was run on the warehouse and
never tuned on returns, backtests or the v1/v2 comparison (a change needs a new ADR).

### Model and features

- Gaussian HMM, 3 states, diagonal covariance, EM (`hmmlearn`), best log-likelihood of
  `n_init` seeded starts. Fits run single-threaded so results are bit-identical between runs.
- Features per NYSE trading day:
  - `spy_vol_60d` = `std(r_{t−59..t}, ddof=1) · √252` on SPY daily simple returns from
    `prices_clean` (`mrm.metrics.risk.rolling_volatility`, the formula of `fact_risk_metrics`)
  - `nfci`, `t10y2y` = as-of values from `macro_asof` (`available_date < trade_date`)
- Standardisation of the official version: expanding `z_t = (x_t − mean(x_1..t)) / std(x_1..t)`,
  NaN until a feature has `zscore_min_obs` values.
- After each fit, states are ordered by their emission mean of `z_spy_vol`: lowest = `calm`,
  highest = `stress`. The names are a convention for a statistical cluster.
- `regime = argmax(p_calm, p_tension, p_stress)`.

### Two variants

| | `point_in_time` (official) | `full_sample` (descriptive) |
|---|---|---|
| `is_point_in_time` | true | **false** |
| Standardisation | expanding | whole sample |
| Fits | first on the day with `min_train_days` valid rows before it, then every `refit_every_days` trading days, each on rows before its refit day | one, on all rows |
| Probabilities | **filtered** `P(s_t \| x_1..x_t)` (numpy forward recursion) | smoothed `P(s_t \| x_1..x_n)` (forward-backward) |
| Use | per-regime analysis, backtests (from `t + 1`) | description only: never performance or backtests |

A row is *valid* when its three z-scores are known. A point-in-time row after the first fit with
a missing feature would be a prediction-only filter step (`p_t = p_{t−1} · A`). The report
counts such rows.

### Warm-up (exact NULL count)

The point-in-time model columns are NULL on the first
`window + (zscore_min_obs − 1) + min_train_days` trading days when SPY has no gaps:
- `window` rows before the first 60-day volatility
- then `zscore_min_obs − 1` rows: the expanding z-score is known from its
  `zscore_min_obs`-th value, the current one included
- then `min_train_days` valid rows before the first fit

`mrm regimes-v2` prints the observed count next to this formula. `tests/test_regimes_v2_real.py`
derives the expected count from the config. Because prices start in January 2007, **the official
version has no label for 2008–09**: the crisis is only in its first training window. The
full-sample version is NULL only on the first `window` rows.

### No look-ahead (tests)

- Row `t` uses SPY closes up to and including `t`, so it is known at the close of `t`. **Act on it
  from `t + 1`.**
- `tests/test_regimes_v2_lookahead.py` (synthetic):
  - Truncation: for several cut dates `D`, including dates just before and just after refits,
    rows `≤ D` are identical when rebuilt from data up to `D`.
  - Changing the future leaves the past unchanged.
  - The last segment equals a forward filter with that segment's fit, and differs from the
    smoothed posteriors.
  - Every training window ends before the day it labels.
- `tests/test_regimes_hmm.py`:
  - The forward filter matches a textbook recursion and hmmlearn's log-likelihood.
  - Filtered probabilities ignore the future; smoothed ones do not.
  - Fits are deterministic.

### Comparison report (`mrm regimes-v2`, aggregates only)

- NULLs per variant and column, with reasons
- fit log: training rows, log-likelihood, EM iterations, convergence, state means
- share of days per regime (each model's days, and the common point-in-time days)
- episode count and mean length
- changes per year, and point-in-time changes that fall on a refit day
- confusion matrices v1 × v2 point-in-time, v1 × v2 full-sample, and v2 point-in-time × full-sample
- agreement per year
- mean SPY volatility per regime
- tension/stress/NULL days inside four dated windows (GFC, COVID, 2022, March 2023; sources in
  the config), as description, not as targets

### Limitations

- **Not a validation.** v1 and v2 have no ground truth. Their agreement, or the overlap with
  dated windows, describes them and scores neither.
- **State names follow volatility only.** The ordering uses the SPY-volatility mean. If two states
  differ mainly on NFCI or the yield curve, `calm` and `tension` can have almost the same
  volatility. The report prints the mean volatility per regime so this is visible. The first-run
  findings are in ADR 0007.
- **Refit discontinuities.** Parameters change once a year, so a label can change on a refit day
  with no change in the data. The report counts these changes.
- **Circularity with the portfolio.** SPY is 40% of the example portfolio and of the 60/40
  benchmark, and its volatility is a feature. Per-regime performance of SPY-heavy portfolios is
  partly mechanical. Report v1 (no SPY input) alongside it.
- **Model assumptions.** Gaussian, diagonal emissions with a constant transition matrix. EM
  finds local optima. Expanding z-scores drift as history accumulates.
- **Inherited data limits.** NFCI is FRED's latest vintage, backcast before 2011-06 (ADR 0005).
