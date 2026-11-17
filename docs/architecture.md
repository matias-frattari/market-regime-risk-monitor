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

## Runtime dependencies

| Library | Why |
|---|---|
| `duckdb` | Embedded analytical warehouse; single file, no server, fast SQL over local data |
| `pyyaml` | Read the editable config files (`config/*.yaml`) |
| `python-dotenv` | Load API keys from the gitignored `.env` (used by the ingestion CLI) |
| `requests` | HTTP client for the Twelve Data and FRED APIs (ingestion) |

Dev: `ruff` (lint/format), `pytest` (tests), `hatchling` (build backend).
