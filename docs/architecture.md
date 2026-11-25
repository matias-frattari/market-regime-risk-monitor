# Architecture

> TODO: full architecture write-up (pipeline diagram, modules, scheduling).

## Ingestion: daily prices (`src/mrm/ingest/`)

| Module | Role |
|---|---|
| `providers.py` | `PriceProvider` protocol, `FetchResult`, `ProviderError` / `RateLimitError`, `redact` |
| `twelvedata.py` | `TwelveDataProvider`: one GET `/time_series` per ticker (`interval=1day`, `adjust=all`, `outputsize=5000`, `order=asc`), validation, raw cache |
| `prices.py` | Orchestration (`ingest_ticker`, `ingest_prices`) and the CLI |

Per ticker: fetch → validate → `replace_prices` (full reload) → `log_ingestion`.

- **Validation** (the ticker fails and stored data is untouched on any of these):
  `meta.symbol` equals the requested ticker, `meta.currency == "USD"`, `meta.exchange` is a
  US venue; dates strictly ascending and unique; close numeric and > 0; newest bar no older
  than 10 calendar days (UTC), which catches a response holding the oldest 5000 bars instead
  of the latest. Rows with a null close are dropped and counted; volume is cast to integer.
- **Partial bar:** if the newest bar is dated today in New York (`America/New_York`) and the
  New York time is before 16:30, it is still an intraday bar and is dropped (logged as
  `dropped partial bar`). Bars of previous days are never dropped. The clock is injectable.
- **Errors:** Twelve Data can return `"status":"error"` with HTTP 200; it is handled like an
  HTTP error. Every request has a timeout. A rate/credit limit (HTTP 429 or API code 429)
  waits 60 s and retries once. Two consecutive tickers still rate-limited after the retry
  abort the run; the remaining tickers are logged as `error` / `aborted: rate limit`.
  One failing ticker never stops the others.
- **Pacing:** 8 s between tickers (free plan: 8 requests/min).
- **API key:** read from `TWELVEDATA_API_KEY` (`.env`). It is sent as a query parameter, so
  URLs, request params and `requests` exception text are never printed, logged or chained;
  messages carry only the error type and HTTP status / API code, and pass through `redact`.
- **Raw cache:** successful responses are written to `data/raw/twelvedata/<TICKER>.json`
  (gitignored; Twelve Data responses do not contain the key).
- **`--dry-run`:** calls the API, validates and writes the raw cache, but writes nothing to
  the warehouse (no `raw_prices`, `dim_asset` or `ingest_log` rows).
- **Exit codes:** 0 no ticker failed, 1 at least one `error`, 2 usage/config error.

## Ingestion: macro series (`src/mrm/ingest/`)

| Module | Role |
|---|---|
| `config/macro.yaml` + `mrm.config.load_macro_series` | Series list and per-series `overlap_days` (`full` = full reload); rejects copyrighted ids (`BLOCKED_SERIES`) |
| `fred.py` | `FredClient`: one GET `series/observations` per series (`file_type=json`, `sort_order=asc`, optional `observation_start`), validation, raw cache |
| `macro.py` | Orchestration (`ingest_series`, `ingest_macro`) and the CLI |

Per series: `macro_fetch_start` (last date minus overlap; None = full history) → fetch →
validate → `upsert_macro` → `log_ingestion`.

- **Validation** (the series fails and nothing is written on any of these):
  - Dates must be strictly ascending.
  - Values must be numeric and finite. FRED's `"."` (missing) is stored as NULL and counted.
  - The response must hold at least the `count` it reports (no truncation).
- **Errors:** FRED error bodies (`error_code`) become `api_error code=<n>`; the message text is
  never copied. HTTP 429 waits 20 s and retries once. One failing series never stops the others.
- **Pacing:** 1 s between series (FRED allows 120 requests/min).
- **API key:** `FRED_API_KEY` (`.env`). It is sent as a query parameter, so it gets the same
  handling as Twelve Data: no URL, params or `requests` exception text is ever printed, logged
  or chained, and messages pass through `redact`.
- **Raw cache:** `data/raw/fred/<ID>.json` (gitignored; FRED responses do not contain the key).
  It holds the last response, which for overlap series is only the window.
- **`--dry-run`:** calls the API (full history, since it does not open the warehouse) and
  validates, but writes nothing to the warehouse.
- **Exit codes:** 0 no series failed; 1 at least one `error`; 2 usage/config error (unknown or
  copyrighted series, missing key).

## Data layers

```
ingest ──► raw_*  (tables)      provider data as loaded; only the ingestion writes here
             │
             ▼
           *_clean (views)       documented, reversible corrections from config/; no imputation
             │                   prices_clean = raw_prices + config/price_adjustments.yaml
             ▼
           *_asof  (views)       point-in-time: no look-ahead + forward-fill
             │                   macro_asof = raw_macro + config/macro_lags.yaml, on the prices_clean trading days
             ▼
   metrics / regimes / portfolio / exports / data quality checks
             │                   metrics -> fact_returns, fact_risk_metrics, fact_correlations
             │                   regimes -> fact_regimes (rules v1, ADR 0006)
             ▼                   (local only; formulas in docs/methodology.md)
   data/exports/*.csv (aggregated metrics only, Phase 5)
```

**Rule: everything after the ingestion reads `*_clean` / `*_asof`, never `raw_*`.**
`raw_*` stays the provider's data so that every correction is visible, testable and can be
reverted by editing config. The data quality checks on prices also read `prices_clean`.
`macro_asof` reads `raw_macro` directly, a documented exception: macro values need no
correction today, so there is no `macro_clean`. It covers all 9 FRED series: a value is used
only after its release, `obs_date + lag_days < trade_date` (lag 0 for the daily Treasury
series, i.e. the Phase 1.6 rule; per-series lags and delayed-release overrides in
`config/macro_lags.yaml`, ADR 0005). Columns and rules: `docs/data_dictionary.md`.

The clean layer is prepared by `mrm.storage.clean.prepare_clean_prices(con)`. It creates the
schema (tables and views) and syncs `config/price_adjustments.yaml` into `price_adjustments`
and `config/macro_lags.yaml` into `macro_publication_lags` / `macro_release_overrides`.
Call it before reading `prices_clean` or `macro_asof`; `mrm check`, `mrm metrics` and
`mrm regimes` do so first. The ingestion does not call it and is unchanged.

**Conditional corrections.** An adjustment applies only while the raw data still shows the
error (raw return on the ex-date above a configured threshold), evaluated inside the view on
every query. If the provider fixes its data, the correction stops applying by itself and
`mrm check` reports a warning. See `docs/decisions/0003-xlf-special-distribution-adjustment.md`.

## Data quality (`src/mrm/quality/`, `uv run mrm check`)

| Module | Role |
|---|---|
| `calendar.py` | NYSE and federal holiday rules (pure Python); special closures come from config |
| `checks.py` | Read-only checks over `prices_clean`, `raw_macro`, `dim_asset`, `price_adjustment_status`; one `Finding` per (check, entity, severity) |
| `report.py` | Console report by severity, summary row, exit code (1 only on critical) |
| `run.py` | CLI: prepares the clean layer, runs the checks, appends to `dq_results` |

Check rules and thresholds: `docs/data_dictionary.md`, "Data quality checks".
`src/mrm/cli.py` dispatches `mrm ingest prices|macro`, `mrm check`, `mrm metrics` and
`mrm regimes` to the module entry points.

## Regimes (`src/mrm/regimes/`, `uv run mrm regimes`)

| Module | Role |
|---|---|
| `rules.py` | Pure functions: NFCI/STLFSI4 levels, backcast/revised label, curve flag, Sahm gap, `build_regimes` |
| `io.py` | Reads NFCI, STLFSI4, T10Y2Y, UNRATE from `macro_asof` (NYSE trading days only); full rebuild of `fact_regimes` in one transaction |
| `report.py` | Aggregates only: shares per regime and flag, changes per year, NFCI vs STLFSI4 agreement, NULLs with reasons, stale NFCI runs vs known events |
| `run.py` | CLI: loads `config/regime_rules.yaml`, prepares the clean layer, builds, writes, prints the report |

Rules, thresholds and limits: ADR 0006 and `docs/methodology.md`, "Regimes v1".

## Runtime dependencies

| Library | Why |
|---|---|
| `duckdb` | Embedded analytical warehouse; single file, no server, fast SQL over local data |
| `pyyaml` | Read the editable config files (`config/*.yaml`) |
| `python-dotenv` | Load API keys from the gitignored `.env` (used by the ingestion CLI) |
| `requests` | HTTP client for the Twelve Data and FRED APIs (ingestion) |

Dev: `ruff` (lint/format), `pytest` (tests), `hatchling` (build backend).
