# ADR 0007: Regimes v2, Gaussian HMM (point-in-time) and comparison against v1

- **Status:** Accepted
- **Date:** 2026-11-25

## Context

The rules-based v1 (ADR 0006) is a transparent baseline built on a single index. On the
2007–2026 warehouse it labels most days `calm`, puts every `stress` day in the NFCI backcast of
2008–09, gives only 6 regime changes, and labels every year since 2010 `calm`, including 2022 and
March 2023. Step 3.2 adds an unsupervised model that also sees market volatility. It then reports
where that model and v1 agree and where they do not. It does not replace v1 and it does not
validate it.

All hyperparameters below were written in `config/regime_model.yaml` **before the model was run
on the warehouse**. They are not tuned on returns, backtests or the v1/v2 comparison. Changing
one requires a new ADR.

## Decision

### Model

- Gaussian hidden Markov model with **3 states** and **diagonal covariance**, fitted by EM
  (`hmmlearn.hmm.GaussianHMM`, `n_iter = 200`, `tol = 1e-4`, k-means initialisation).
- Each fit is run `n_init = 10` times with seeds `seed + i` (`seed = 0`). The run with the
  highest log-likelihood is kept, so results are deterministic.
- **States are ordered after every fit** by their emission mean of the SPY-volatility feature:
  lowest = `calm`, middle = `tension`, highest = `stress`. This prevents label switching
  between refits. The names are a convention: a state is a statistical cluster, not an
  economic diagnosis.

### Features (one row per NYSE trading day of `prices_clean`)

| Feature | Source | Known at |
|---|---|---|
| `spy_vol_60d` | Annualised 60-day realised volatility of SPY daily simple returns: `mrm.metrics.risk.rolling_volatility` over `mrm.metrics.returns.simple_returns` of `prices_clean` (same formula as `fact_risk_metrics`, not re-implemented) | close of `t` |
| `nfci` | `macro_asof` value of NFCI (`available_date < trade_date`, ADR 0005) | before `t` |
| `t10y2y` | `macro_asof` value of T10Y2Y (strict `obs_date < trade_date`) | before `t` |

- **Standardisation.** The official version uses expanding windows:
  `z_t = (x_t − mean(x_1..t)) / std(x_1..t, ddof = 1)` per feature, over its non-missing values.
  `z_t` is NaN until the feature has `zscore_min_obs = 60` observations, the current one
  included. Such rows never enter a fit. The full-sample version uses the mean and standard
  deviation of the whole sample.
- VIXCLS and BAMLH0A0HYM2 stay excluded (ADR 0002). STLFSI4 and UNRATE are not features. They
  remain v1 robustness checks and flags.

### Official version: point-in-time (`variant = 'point_in_time'`, `is_point_in_time = true`)

- A row is **valid** when all three z-scores are known.
- **First fit** `R_0` is the first trading day with `min_train_days = 756` valid rows strictly
  before it.
- **Refits** happen every `refit_every_days = 252` trading days after that: `R_k = R_0 + 252·k`,
  counted in positions of the trading-day grid. Fit `k` trains on the valid rows with
  `trade_date < R_k`, standardised as they were known on each row. It is applied to the days
  `[R_k, R_{k+1})`.
- **Filtered probabilities only.** For a day `t` in segment `k`, `p_state = P(s_t | x_1..x_t; θ_k)`
  comes from a forward recursion over the valid rows up to `t`, started on the first valid row
  with the fitted initial distribution. Neither Viterbi nor smoothed (forward-backward)
  posteriors are used, because both depend on observations after `t`. A row that has a missing
  feature after the warm-up becomes a prediction-only step: it gets no emission term and is
  counted in the report.
- `regime = argmax(p_calm, p_tension, p_stress)`.
- **Expected NULLs.** All columns that depend on the model are NULL on the first
  `window + (zscore_min_obs − 1) + min_train_days` trading days, provided SPY has no gaps. The
  60-day volatility needs 60 returns, the expanding z-score is known from its 60th observation,
  and 756 valid rows precede `R_0`. The exact count and its date range are printed by
  `mrm regimes-v2`. The real-warehouse test derives the expected count from the config instead of
  hard-coding it.

### Descriptive version: full sample (`variant = 'full_sample'`, `is_point_in_time = false`)

- One fit on all valid rows, with full-sample standardisation and **smoothed** posteriors
  (forward-backward). It is the "in hindsight" view.
- **Not point-in-time.** It uses information from the whole sample on every day. It must never
  enter a performance comparison, a backtest or a per-regime return table. It exists only to
  show how the model would segment history with every observation available, including
  2008–09.
- NULL only on the `window` rows before the first 60-day volatility.

### Storage and command

- `fact_regimes_v2` is in long format with primary key `(trade_date, variant)`: one row per
  trading day per variant. It holds the probabilities per state, the label, the raw and
  standardised features, `train_end_date` and `train_rows`. It is local only: nothing is exported
  in this step.
- `uv run mrm regimes-v2 [--dry-run]` recomputes v1 in memory from the same `macro_asof` inputs
  and prints aggregates only:
  - NULLs per variant
  - the refit log
  - share of days per regime and mean episode length
  - changes per year, and how many changes fall on a refit day
  - confusion matrices v1 × v2 and agreement per year
  - tension/stress days inside four dated windows (GFC, COVID, 2022, March 2023)

  The windows are descriptive, with sources in the config. They are not targets.

## No look-ahead

1. Macro inputs come from `macro_asof` (publication lags, ADR 0005). SPY volatility at `t` uses
   closes up to and including `t`. **The regime of day `t` is therefore known only at the close
   of `t`**: any consumer, such as the Phase 4 backtest, must act on it from `t + 1`.
2. Expanding standardisation uses rows `≤ t`. The refit calendar depends only on row positions,
   and every fit uses rows `< R_k`.
3. Probabilities are filtered, never smoothed or Viterbi.
4. Tests (synthetic data):
   - **Truncation.** For several cut dates `D`, including dates just before and just after a
     refit, every row with `trade_date ≤ D` is identical when the table is rebuilt from data up
     to `D`.
   - **Filtering.** Changing observations after `t` leaves every probability at `≤ t`
     unchanged, while the smoothed posteriors of the same model do change.
   - **Reproducibility.** Two runs give bit-identical results.

## Warm-up: no point-in-time labels for 2008–09

`prices_clean` starts in January 2007, so the official version's warm-up runs into 2010. The
volatility window, the z-score minimum and 756 training days add up to about three and a half
years. **The point-in-time model does not label the 2008–09 crisis.** That period is only in
its first training set. The v1 × v2 point-in-time comparison therefore excludes the only years
where v1 shows `stress`. The full-sample version covers 2008–09, but in hindsight. A longer SPY
history would shorten this gap. That is a data change, not a hyperparameter change.

## Circularity with the portfolio

SPY is 40% of the example portfolio (`config/portfolio.example.yaml`) and the 60/40 benchmark,
and SPY's own realised volatility is a model input. A `stress` state will coincide with SPY
drawdowns **partly by construction**, so per-regime performance in Phase 4 will flatter the
regime signal for SPY-heavy portfolios. Mitigations: report v1, which has no SPY input, next to
v2; lag the regime by one day (point 1 above); and state the circularity next to every per-regime
table.

## Limitations (the HMM is not a validation)

1. **Agreement is not validation.** v1 and v2 are both models with no ground truth. Agreement
   between them, or with dated windows, is described, not scored, and is never used to choose
   hyperparameters.
2. **Model assumptions.** Gaussian emissions with diagonal covariance and a first-order Markov
   chain with a constant transition matrix are simplifications. Volatility is skewed and
   persistent, so a state can capture a volatility level rather than a "regime".
3. **Refit discontinuities.** Parameters change once a year, so a label can change on a refit
   day with no change in the data. The report counts these changes.
4. **Local optima.** EM finds local maxima. Ten seeded starts reduce this, but do not remove it.
5. **Non-stationary standardisation.** Expanding z-scores drift as history accumulates. Early
   z-scores rest on few observations, which is the reason for the 60-observation minimum.
6. **Inherited data limits.** NFCI is FRED's latest vintage and is backcast before 2011-06
   (ADR 0005, ADR 0006). The model inherits this revision look-ahead. The SPY volatility
   feature does not have it.

## Dependencies

- **hmmlearn** (BSD): maintained Gaussian HMM with EM and C-level forward-backward. It saves a
  hand-written EM implementation and its tests.
- It brings **scipy** (BSD), used by hmmlearn, and **scikit-learn** (BSD), whose k-means is
  used for initialisation, plus their transitive packages. All are free and pinned in `uv.lock`.
- The project uses hmmlearn only to fit. The forward filter is ~25 lines of numpy in
  `mrm.regimes.hmm`, so the official version does not depend on hmmlearn's smoothed
  `predict_proba`.

## Alternatives considered

- **GMM** (scikit-learn): it has no temporal persistence, so it would flip more and has no
  transition matrix to report. The HMM is the natural model for regimes that persist.
- **Full covariance:** more parameters (6 instead of 3 per state) and less stable early fits.
  Diagonal covariance was fixed a priori.
- **Daily or quarterly refits:** heavier and with more refit
  discontinuities. Yearly refits are the common convention.
- **Smoothed or Viterbi labels for the official version:** both use future observations, so
  they are rejected (look-ahead). They are kept only for the descriptive full-sample version.
- **Own EM implementation:** no new dependency, but more code to test. It remains the fallback
  if hmmlearn stops installing on CI.
