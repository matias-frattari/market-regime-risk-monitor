# Data Dictionary

Warehouse: DuckDB file at `$MRM_DB_PATH` (default `data/warehouse.duckdb`, gitignored).
Schema is created idempotently by `mrm.storage.schema.create_schema`.

Conventions:
- All `TIMESTAMP` columns hold **naive UTC** values, written by the loaders (`utc_now()`).
- No foreign keys (in DuckDB they would block updates of `dim_asset`); integrity is checked
  in the loaders and tests.
- **Licensing:** `raw_prices` stays in the local warehouse only. Prices and daily per-ticker
  returns are never exported or committed (see `docs/decisions/0001-price-provider.md`).

## Warehouse tables

### `dim_asset`
One row per ticker of `config/universe.yaml`. PK: `ticker`.
Kept equal to the universe by `sync_assets` (run by the price ingestion CLI): tickers removed
from the YAML are deleted from `dim_asset` in the same transaction as the upsert; their
`raw_prices` and `ingest_log` rows are not touched.

> **EEM → VWO (2026-11-17).** EEM was dropped: Twelve Data returned a mixed series for it
> (1494 dates duplicated with different values, history only from 2012), with and without
> `mic_code=ARCX`. It was replaced by VWO (FTSE Emerging Markets), which passed validation.
> See the note in `docs/decisions/0001-price-provider.md`.

| Column | Type | Null | Description |
|---|---|---|---|
| ticker | VARCHAR | no | ETF symbol, upper case |
| asset_class | VARCHAR | no | Top-level YAML group: `equity_us`, `equity_intl`, `bonds`, `commodities`, `sectors` |
| updated_at | TIMESTAMP | no | Last upsert (UTC) |

### `raw_prices`
Daily adjusted bars from Twelve Data `/time_series` (`interval=1day`, `adjust=all`).
PK: `(ticker, trade_date)`.

| Column | Type | Null | Description |
|---|---|---|---|
| ticker | VARCHAR | no | ETF symbol |
| trade_date | DATE | no | Trading day |
| open, high, low | DOUBLE | yes | Adjusted by the provider (`adjust=all`); **not** verified empirically |
| close | DOUBLE | no | Adjusted close (`adjust=all`); the only field verified empirically (ADR 0001) |
| volume | BIGINT | yes | As returned by the provider |
| source | VARCHAR | no | `twelvedata` |
| ingested_at | TIMESTAMP | no | Load time (UTC) |

**Load strategy — full reload per ticker.** `adjust=all` re-adjusts the whole history after
every dividend/split, and the provider's 5000-bar window moves forward over time, so an
incremental upsert would leave stale or out-of-window rows. Ingestion therefore calls
`replace_prices(con, ticker, bars)`: in one transaction it deletes all rows of the ticker and
inserts the new bars. If `bars` is empty (empty response or error) **nothing is deleted**.
`upsert_prices` (upsert by PK) exists for generic use and tests only. `last_price_date` is
for diagnostics/logging, not for deciding what to fetch.

### `raw_macro`
FRED observations. PK: `(series_id, obs_date)`.

| Column | Type | Null | Description |
|---|---|---|---|
| series_id | VARCHAR | no | FRED id: DGS10, DGS2, T10Y2Y, DGS3MO, FEDFUNDS, CPIAUCSL, UNRATE, VIXCLS, BAMLH0A0HYM2 |
| obs_date | DATE | no | Observation date |
| value | DOUBLE | yes | NULL when FRED reports `.` (missing) |
| source | VARCHAR | no | `fred` |
| ingested_at | TIMESTAMP | no | Load time (UTC) |

**Load strategy — incremental with overlap.** FRED revises recent data, so each run requests
from `macro_fetch_start(con, series_id)` = last stored `obs_date` minus 90 days (full history
when empty) and upserts by PK (`upsert_macro`). Rows with NULL `value` count as already fetched.

### `ingest_log`
One row per ingestion attempt per ticker/series. PK: `log_id` (sequence `ingest_log_seq`).

| Column | Type | Null | Description |
|---|---|---|---|
| log_id | BIGINT | no | Surrogate key |
| source | VARCHAR | no | `twelvedata` or `fred` |
| entity | VARCHAR | no | Ticker or FRED series id |
| date_from, date_to | DATE | yes | Date range loaded |
| rows_loaded | INTEGER | no | Rows written |
| status | VARCHAR | no | `success`, `no_data` or `error` (CHECK constraint) |
| message | VARCHAR | yes | Error detail; never contains URLs or API keys |
| logged_at | TIMESTAMP | no | Attempt time (UTC) |

Price ingestion (`mrm.ingest.prices`) writes one row per ticker and run:
- `success`: bars loaded; `date_from`/`date_to` = first/last bar; `message` notes rows dropped
  for a null close and/or `dropped partial bar` (today's intraday bar, before 16:30 New York),
  joined by `; `, if any.
- `no_data`: empty response; nothing deleted, dates NULL.
- `error`: nothing deleted, dates NULL, `rows_loaded = 0`. `message` is one of: exception type
  plus HTTP status (`ConnectionError status=N/A`), `HTTP <code>`, `api_error code=<code>`
  (the API's text is not copied), `invalid meta: <field>=<value>`, `invalid close on <date>`,
  `dates not strictly ascending at <date>`, `stale data: last date <date>`,
  `aborted: rate limit`, or a bare exception type name for unexpected errors.

## CSV exports (`data/exports/`)

> TODO: document Tableau export schemas (later phases).
