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
| series_id | VARCHAR | no | FRED id from `config/macro.yaml`: DGS10, DGS2, T10Y2Y, DGS3MO, FEDFUNDS, CPIAUCSL, UNRATE, NFCI, STLFSI4 |
| obs_date | DATE | no | Observation date |
| value | DOUBLE | yes | NULL when FRED reports `.` (missing) |
| source | VARCHAR | no | `fred` |
| ingested_at | TIMESTAMP | no | Load time (UTC) |

`obs_date` is FRED's observation date (weekly NFCI/STLFSI4: the week's Friday; monthly
series: the 1st of the month), **not** the publication date. Values are the latest vintage.

> VIXCLS and BAMLH0A0HYM2 are copyrighted and never ingested
> (`docs/decisions/0002-macro-series-licensing.md`).

**Load strategy: incremental with overlap, per series (`overlap_days` in `config/macro.yaml`).**
FRED revises data, so each run requests from `macro_fetch_start(con, series_id, overlap_days)`.
That is the last stored `obs_date` minus the overlap window (90 days by default). The full
history is requested when the table is empty, and on every run for `overlap_days: full`
(NFCI, STLFSI4, whose whole past is revised). Rows are upserted by PK (`upsert_macro`). Rows
with NULL `value` count as already fetched. Nothing is deleted.

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

Macro ingestion (`mrm.ingest.macro`) writes one row per series and run (`source = fred`):
- `success`: observations upserted. `rows_loaded` includes rows with a NULL value.
  `date_from`/`date_to` are the first/last observation received, not the full stored range.
  `message` can hold `N missing values ('.')` and/or `full reload`, joined by `; `.
- `no_data`: empty response, nothing written, dates NULL.
- `error`: nothing written, dates NULL, `rows_loaded = 0`. `message` is one of:
  - exception type plus HTTP status (`Timeout status=N/A`)
  - `HTTP <code>`
  - `api_error code=<code>` (FRED's text is not copied)
  - `invalid JSON`
  - `invalid value on <date>`
  - `dates not strictly ascending at <date>`
  - `truncated response`
  - a bare exception type name for unexpected errors.

### `dq_results`
One row per data quality finding per run of `uv run mrm check`. Append-only.
PK: `(run_id, check_name, entity, severity)`.

| Column | Type | Null | Description |
|---|---|---|---|
| run_id | VARCHAR | no | Random id of the run (12 hex chars), printed by the CLI |
| run_at | TIMESTAMP | no | Run time (UTC) |
| check_name | VARCHAR | no | Check id (see below), or `run` for the per-run summary row |
| entity | VARCHAR | no | Ticker, FRED series id, `<table>:<entity>` for `future_dates`, `all` for `run` |
| severity | VARCHAR | no | `critical`, `warning` or `info` (CHECK constraint) |
| n_affected | INTEGER | no | Dates/rows involved (days of lag for staleness, age in days for freshness) |
| detail | VARCHAR | yes | Dates and counts, truncated after 10 items; never URLs or API keys |

Findings are aggregated per `(check, entity, severity)`. For example, all short price gaps of
one ticker form one warning row and the dates go in `detail`. Every run also writes one
`run` / `all` / `info` row with the counts per severity, so clean runs are recorded too.
Kept separate from `ingest_log` because the grain (a finding, not a load attempt) and the
status values (severities) differ.

## Data quality checks (`uv run mrm check`)

Checks only read `raw_prices`, `raw_macro` and `dim_asset`. They never impute or modify data:
exceptions in the config only reclassify a finding (warning → info).
Thresholds and exceptions: `config/data_quality.yaml`. Code: `src/mrm/quality/`.
"Today" is the current date in New York. Exit code: `1` if any finding is critical, else `0`
(`2` for an invalid config). `--no-save` prints the report without writing `dq_results`.

| Check | Table | Severity | Rule (config key) |
|---|---|---|---|
| `price_duplicates` | raw_prices | critical | More than one row per `(ticker, trade_date)`. The PK prevents it today; the check guards against schema changes |
| `price_non_positive` | raw_prices | critical | `open`, `high`, `low` or `close` ≤ 0 |
| `price_missing_dates` | raw_prices | critical / warning | NYSE trading days missing between each ticker's **own** first and last date. A run of consecutive missing days longer than `prices.max_missing_gap_days` is critical, shorter runs are warnings |
| `price_non_trading_dates` | raw_prices | warning | Bars dated on a weekend, an NYSE holiday or a special closure |
| `price_extreme_returns` | raw_prices | warning / info | \|close/prev close − 1\| > `prices.extreme_return_abs`. Inside a `prices.known_extreme_returns` window it is info. Detail: count plus the `extreme_return_top_n` largest moves |
| `price_staleness` | dim_asset + raw_prices | critical | Last bar more than `prices.stale_after_business_days` NYSE trading days behind the **last trading day** (a Saturday run compares against Friday). A universe ticker with no bars is critical too |
| `macro_freshness` | raw_macro | critical / warning | Last **non-missing** observation older than `macro.max_age_days[<frequency>]` calendar days (frequency from `macro.series_frequency`). A configured series with no data is critical. A stored series without a configured frequency is a warning |
| `macro_gaps` | raw_macro | info / warning | Each FRED missing value (`'.'`, stored as NULL) is classified; see below |
| `future_dates` | both | critical | `trade_date` or `obs_date` after today |

**Classification of FRED missing values (`macro_gaps`).** Nothing is imputed: NULLs stay NULL.
1. Listed in `macro.known_gaps` → info (*known gap*, with its reason). Today:
   CPIAUCSL and UNRATE on 2025-10-01. The federal shutdown of Oct–Nov 2025 meant the BLS did
   not publish October.
2. Daily series only:
   - Dated before `macro.classify_gaps_from` (1986-01-01) → info, counted in aggregate. The
     bond-market holidays of that era (Lincoln's Birthday, Election Day, fixed-date holidays
     before 1971) are not modelled by the rules.
   - On a weekend, a US federal holiday, an NYSE holiday (incl. Good Friday) or a
     `calendar.special_closures` date → info (*holiday*). Example: Veterans Day, when the
     NYSE is open but the bond market is closed.
3. Anything else → warning (*unexplained*). Weekly and monthly series are dated on fixed days
   (Friday, the 1st of the month), so a holiday never explains their missing values.

**Calendar.** `mrm.quality.calendar` holds NYSE and federal holiday rules in pure Python
(1971+ rules; Juneteenth on the NYSE from 2022, MLK Day from 1998). One-off closures, such as
national days of mourning and hurricanes, live in `calendar.special_closures`.
`tests/test_quality_calendar_spy.py` validates the rules against the real SPY dates in the
local warehouse. Over 2007-01-04 → 2026-11-17 the only differences are the 4 listed special
closures. The test is skipped when the warehouse is absent (CI).

**Known finding (2026-11-18).** XLF +19.56% on 2016-09-19 is reported as a warning. That is
the date the Real Estate sector was spun off into XLRE, through a special distribution. The
`adjust=all` close from Twelve Data does not seem to absorb it. It is left visible on purpose,
not added as an exception.

## Macro look-ahead risk (documentation only)

`raw_macro.obs_date` is FRED's **reference** date, not the date the value became public:

| Series | `obs_date` | Typically published |
|---|---|---|
| CPIAUCSL | 1st of the reference month | ~2–6 weeks after the month ends (mid-month for CPI) |
| UNRATE | 1st of the reference month | First Friday of the following month |
| FEDFUNDS | 1st of the reference month | Early in the following month |
| NFCI | Friday of the reference week | The following Wednesday |
| STLFSI4 | Friday of the reference week | The following week |
| DGS10, DGS2, DGS3MO, T10Y2Y | Trading day | Next business day |

Joining these series to prices on `obs_date` uses information **before** it was available
(look-ahead bias). FRED also stores only the latest vintage, so revisions are a residual
look-ahead (`docs/methodology.md`). The publication lag applied per series is decided in the
regime-model phase, not here.

**Pending:** a forward-filled, publication-lagged macro view (no look-ahead) does **not**
exist yet. It will be created with the lag decision above.

## CSV exports (`data/exports/`)

> TODO: document Tableau export schemas (later phases).
