# ADR 0001: Daily price provider

- **Status:** Accepted
- **Date:** 2026-11-17

## Context

The pipeline needs daily, dividend/split-adjusted prices for ~13 US-listed ETFs
(`config/universe.yaml`), with enough history for walk-forward estimation and regime
analysis, from a free tier. All access goes through a `PriceProvider` interface, so the
provider can be swapped later. Candidates from the project brief: Alpha Vantage,
Twelve Data, Alpaca.

Evaluation was done with a temporary, untracked script (`scripts/test_providers.py`)
using real free-tier API keys. No API key, URL or raw response is stored in the repo.

## Evidence

### Twelve Data (free plan)

| Aspect | Finding |
|---|---|
| Endpoint | `/time_series`, `interval=1day`, `adjust=all` |
| History | ≥ ~20 years (limited by `outputsize=5000`; more with date-range pagination) |
| Adjustment (`adjust=all`) | **Verified** empirically on 2026-11-17: SPY, 2010-01-04, adjusted/raw close ratio = 0.749 (adjusted < raw, as expected from dividends) |
| Rate limit | 8 requests/min |

Scope of the adjustment check: one ticker (SPY) and one old date; other tickers were not
checked individually.

### Alpha Vantage (free plan)

- `TIME_SERIES_DAILY_ADJUSTED` (calls 1–2) returned a premium-plan message on the first
  run (session observation; output not saved).
- Plan B (`TIME_SERIES_DAILY` unadjusted + `DIVIDENDS` + `SPLITS`, rebuilding the
  adjustment locally): calls 3 and 4 failed because of the **1 request/second** limit
  (the script had no pause between calls), not because of the daily quota.
- **Plan B: not evaluated.** A fair test needs ≥ 1 s spacing between calls.

### Alpaca

Not evaluated empirically (documentation only; no API calls).

## Feasibility

With 8 req/min on Twelve Data, one request per ticker must be spaced ~8 s apart:
13 tickers ≈ 100 s for a full load. A daily incremental refresh (one small request per
ticker) has the same ~100 s cost and fits comfortably in a scheduled CI job.

## Data licensing / usage

Source: Twelve Data Terms of Service, https://twelvedata.com/terms (consulted 2026-11-17).

- **§2.2(e):** redistribution of raw data is prohibited.
- **§2.2(c):** publishing *Derived Data* is allowed when the original data cannot be
  reconstructed from it.
- **§2.3(l):** the free (Basic) plan may not be used commercially. This project is
  educational and non-commercial.
- **§17.1(g):** ambiguous; read conservatively.

**Project rule:** publish only aggregated metrics (e.g. annualized volatility, drawdown,
VaR/CVaR, correlations, strategy/backtest results). Never publish prices or daily
per-ticker returns, in `data/exports/`, in docs or anywhere else in the repo.
Raw responses stay in gitignored `data/raw/` and the local warehouse.

## Decision

Use the **Twelve Data free plan** (`/time_series`, `interval=1day`, `adjust=all`) as the
daily price provider, behind the `PriceProvider` interface.

Rationale: it is the only candidate empirically verified to return dividend-adjusted
prices on the free plan, and its limits are enough for the 13-ticker universe.

## Consequences

1. Ingestion goes behind the `PriceProvider` interface and caches raw responses
   locally (gitignored).
2. Loads are idempotent but **not** incremental for prices: with `adjust=all` the whole
   history is re-adjusted after every dividend/split, and the 5000-bar window moves
   forward. Each run reloads every ticker in full and replaces its stored history in one
   transaction (delete + insert; an empty response deletes nothing). The cost is the same
   one request per ticker. *(Amended in Phase 1.1; originally "only dates after the last
   stored one are requested".)*
3. The client must space requests ~8 s apart (8 req/min): ≈ 100 s per run for
   13 tickers. A 1 s pause is **not** enough.
4. Tableau exports must follow the licensing rule above.
5. Fallback: Alpaca (evaluated from documentation only) or Alpha Vantage with the
   adjustment rebuilt locally (not evaluated). Revisit this decision if Twelve Data's
   limits or terms change.

## Note (Phase 1.2, 2026-11-17): EEM replaced by VWO

During the first full load, Twelve Data returned a mixed series for **EEM**: 5000 bars with
only 3506 distinct dates (1494 dates duplicated between 2013-03-11 and 2021-11-05, almost all
with different values), so history started in 2012-12 instead of 2007. The symbol is
ambiguous on the provider side, and requesting it with `mic_code=ARCX` returned the same
mixed series. Ingestion validation rejected it and nothing was loaded.

EEM was replaced by **VWO** (Vanguard FTSE Emerging Markets, same `equity_intl` group), which
passed validation: 5000 bars, unique ascending dates from 2007-01-04, no null closes, meta
`USD` / `NYSE` (`ARCX`). IEMG was also clean but its history starts in 2012. Diagnostics
inspected dates and metadata only; no prices were printed or stored in the repo.
