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
  history. The decision is revisited when planning step 3.1. See
  `docs/decisions/0005-macro-publication-lags.md` and
  `docs/decisions/0002-macro-series-licensing.md`.
