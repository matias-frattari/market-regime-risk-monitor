# Data Dictionary

Warehouse: DuckDB file at `$MRM_DB_PATH` (default `data/warehouse.duckdb`, gitignored).
Schema is created idempotently by `mrm.storage.schema.create_schema`.

Conventions:
- All `TIMESTAMP` columns hold **naive UTC** values, written by the loaders (`utc_now()`).
- No foreign keys (in DuckDB they would block updates of `dim_asset`); integrity is checked
  in the loaders and tests.
- **Licensing:** `raw_prices` stays in the local warehouse only. Prices and daily per-ticker
  returns are never exported or committed (see `docs/decisions/0001-price-provider.md`).
  The `fact_*` metric tables are local too; see "Metric tables" for what may be exported later.

## Data layers

| Layer | Objects | Rule |
|---|---|---|
| `raw_*` | tables `raw_prices`, `raw_macro` | Provider data exactly as loaded. Only the ingestion writes here |
| `*_clean` | view `prices_clean` | Documented, reversible corrections (from `config/`). One row per raw row, nothing imputed |
| `*_asof` | view `macro_asof` | Point-in-time: no look-ahead (publication lags of `config/macro_lags.yaml`), forward-fill, aligned to the trading days of `prices_clean` |

Everything downstream (metrics, regimes, portfolio, exports, data quality checks on prices)
reads `*_clean` / `*_asof`, **never** `raw_*`.

**Documented exception:** `macro_asof` reads `raw_macro` directly. There is no `macro_clean`
because macro values need no correction today. If a macro correction is ever needed, a
`macro_clean` view goes between them and `macro_asof` reads from it instead.

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
| entity | VARCHAR | no | Ticker, FRED series id, `<table>:<entity>` for `future_dates` (`prices_clean:SPY`, `raw_macro:NFCI`), `all` for `run` |
| severity | VARCHAR | no | `critical`, `warning` or `info` (CHECK constraint) |
| n_affected | INTEGER | no | Dates/rows involved (days of lag for staleness, age in days for freshness) |
| detail | VARCHAR | yes | Dates and counts, truncated after 10 items; never URLs or API keys |

Findings are aggregated per `(check, entity, severity)`. For example, all short price gaps of
one ticker form one warning row and the dates go in `detail`. Every run also writes one
`run` / `all` / `info` row with the counts per severity, so clean runs are recorded too.
Kept separate from `ingest_log` because the grain (a finding, not a load attempt) and the
status values (severities) differ.

### `price_adjustments`
Documented corrections of provider-adjusted prices, synced from `config/price_adjustments.yaml`
by `mrm.storage.clean.prepare_clean_prices` (delete + insert, one transaction; run by
`mrm check`). The yaml is the source of truth and holds the derivation and sources; the table
holds only what the views need. PK: `adjustment_id`. Decision:
`docs/decisions/0003-xlf-special-distribution-adjustment.md`.

| Column | Type | Null | Description |
|---|---|---|---|
| adjustment_id | VARCHAR | no | Id from the yaml (e.g. `xlf-2016-09-19-xlre-spinoff`) |
| ticker | VARCHAR | no | Ticker to correct |
| ex_date | DATE | no | Prices strictly **before** this date are multiplied |
| multiplier | DOUBLE | no | Factor for open/high/low/close (> 0, CHECK constraint) |
| apply_if_raw_return_above | DOUBLE | no | Apply only while the raw return on `ex_date` is above this |
| synced_at | TIMESTAMP | no | Sync time (UTC) |

Removing an entry from the yaml and running `mrm check` reverts the correction; `raw_prices`
is never modified.

### `macro_publication_lags`
Publication lag per macro series, synced from `config/macro_lags.yaml` by
`mrm.storage.clean.prepare_clean_prices` (delete + insert together with
`macro_release_overrides`, one transaction; run by `mrm check` and `mrm metrics`). The yaml is
the source of truth and holds the sources; the yaml must list exactly the series of
`config/macro.yaml`. `macro_asof` exposes exactly the series of this table. PK: `series_id`.
Decision: `docs/decisions/0005-macro-publication-lags.md`.

| Column | Type | Null | Description |
|---|---|---|---|
| series_id | VARCHAR | no | FRED series id |
| frequency | VARCHAR | no | `daily`, `weekly` or `monthly` (CHECK) |
| lag_days | INTEGER | no | Days from `obs_date` to the release (≥ 0, CHECK); 0 for the daily Treasury series |
| synced_at | TIMESTAMP | no | Sync time (UTC) |

### `macro_release_overrides`
Documented delayed first releases: the date replaces `obs_date + lag_days` for one
observation. Synced with `macro_publication_lags`. The loader rejects an override that is not
later than `obs_date + lag_days` (overrides only delay). PK: `(series_id, obs_date)`.

| Column | Type | Null | Description |
|---|---|---|---|
| series_id | VARCHAR | no | FRED series id |
| obs_date | DATE | no | Reference date of the delayed observation |
| available_date | DATE | no | First-release date on ALFRED (> `obs_date`, CHECK) |
| reason | VARCHAR | no | Cause of the delay (marked "not verified" when unconfirmed) |
| synced_at | TIMESTAMP | no | Sync time (UTC) |

Today: 9 series, 8 overrides (CPIAUCSL 1, UNRATE 3, NFCI 4).

### Metric tables (Phase 2, local only)

Built by `uv run mrm metrics` (`mrm.metrics.run`) from `prices_clean` and DGS3MO in
`macro_asof`; formulas in `docs/methodology.md`. Each run is a full rebuild in one transaction:
asset rows, the run's portfolio rows and all correlations are deleted and re-inserted; rows of
other portfolios are kept. Those kept rows were computed on older prices (a price reload with
`adjust=all` re-adjusts the whole history), so they can disagree with the rebuilt asset rows: the
run prints `warning: portfolio rows older than the asset rows: <ids>` (in a dry run, every other
portfolio id present). Re-run `mrm metrics --portfolio PATH` for each listed id.
`computed_at` (naive UTC) is the run time. NaN results are never
stored: a missing row means "not computable" (incomplete window, no data, zero dispersion).

**Not exported.** These tables stay in the gitignored warehouse. `fact_returns` holds daily
per-ticker returns and is never exported. Per-ticker daily drawdown (`fact_risk_metrics`,
`metric = 'drawdown'`, `entity_type = 'asset'`) and the daily rolling correlations in
`fact_correlations` are derived from licensed prices and **need a licensing review before any
export in Phase 5** (ADR 0001). Only aggregated metrics are planned for the Tableau CSVs.

**Entity ids.** `entity_id` is the ticker (`asset`), the portfolio id from the portfolio yaml
(`portfolio`), or `'<portfolio_id>:<ticker>'` for a holding's Euler contribution (`holding`).
Chosen over a `portfolio_id` column because DuckDB primary-key columns cannot be NULL (asset
rows would need a sentinel), while the prefix keeps the existing key unique across portfolios
with no extra column. Portfolio ids are restricted to `[a-z0-9_-]`, so `:` is unambiguous.

#### `fact_returns`
Daily returns per asset and per portfolio. PK: `(entity_id, trade_date)`.

| Column | Type | Null | Description |
|---|---|---|---|
| entity_id | VARCHAR | no | Ticker or portfolio id |
| entity_type | VARCHAR | no | `asset` or `portfolio` (CHECK) |
| trade_date | DATE | no | End day of the return; the start is the previous calendar row (union of `prices_clean` dates filtered to NYSE trading days) |
| simple_return | DOUBLE | no | `P_t / P_{t-1} − 1`; portfolio: `Σ w_i r_i` (daily-rebalanced fixed weights) |
| log_return | DOUBLE | no | `ln(P_t / P_{t-1})`; portfolio: `ln(1 + r_p)` |
| computed_at | TIMESTAMP | no | Run time (UTC) |

No row for the first day of a ticker, for a day without a quote, or for the day after it (a
return never spans a gap). The portfolio has no row on days where any holding lacks a return.

#### `fact_risk_metrics`

> ⚠️ **`window_label = 'full'` values are NOT point-in-time. Never join them by date.**
> They are computed on the **whole history** and stamped with the last return date as
> `as_of_date`. Joining them to `fact_regime`, a backtest or any other table on a date (or
> filtering them "as of" an earlier date) uses future data for every date before `as_of_date`.
> For point-in-time use, take the rolling (`60d`, `126d`) or `expanding` rows, which use only
> data up to `as_of_date`.

Tidy (long) risk metrics. PK: `(entity_id, as_of_date, metric, window_label)`.

| Column | Type | Null | Description |
|---|---|---|---|
| entity_id | VARCHAR | no | See "Entity ids" |
| entity_type | VARCHAR | no | `asset`, `portfolio` or `holding` (CHECK) |
| as_of_date | DATE | no | Last date of data used. `full` metrics: the entity's last return date |
| metric | VARCHAR | no | See the table below |
| window_label | VARCHAR | no | `full` (whole history), `60d` / `126d` (trailing window ending at `as_of_date`), `expanding` (from the first return to `as_of_date`) |
| value | DOUBLE | no | Metric value (units below) |
| n_obs | INTEGER | no | Returns used: window length, or count of non-missing returns (Sharpe: excess returns with a risk-free value). Asset `ann_return`, `max_drawdown` and `drawdown`: return intervals spanned since the first close (gaps included) |
| computed_at | TIMESTAMP | no | Run time (UTC) |

| metric | window_label | entity_type | Grain | Unit |
|---|---|---|---|---|
| ann_return | full | asset, portfolio | one row | annualized geometric return (decimal) |
| ann_vol | full | asset, portfolio | one row | annualized volatility (decimal) |
| max_drawdown | full | asset, portfolio | one row | most negative drawdown (≤ 0) |
| sharpe | full | asset, portfolio | one row | annualized Sharpe ratio, rf = DGS3MO; arithmetic mean excess return, so it can be > 0 while `ann_return` (geometric) is below the average rf (methodology.md) |
| var_hist_95, cvar_hist_95 | full | asset, portfolio | one row | 1-day historical VaR/CVaR 95%, positive loss |
| var_param_95, cvar_param_95 | full | asset, portfolio | one row | 1-day Gaussian VaR/CVaR 95%, positive loss |
| vol_60d | 60d | asset, portfolio | daily | annualized rolling volatility |
| drawdown | expanding | asset, portfolio | daily | drawdown from the running peak (≤ 0); asset: from the close level, portfolio: from returns |
| risk_contrib | 126d | holding | daily | Euler contribution, annualized volatility units; sums to the portfolio's 126d volatility |
| risk_contrib_pct | 126d | holding | daily | share of portfolio volatility; sums to 1 per date |

#### `fact_correlations`
Rolling pairwise Pearson correlation of daily simple returns. PK:
`(ticker_a, ticker_b, trade_date, window_label)`; CHECK `ticker_a < ticker_b` (each pair
once) and `correlation` in [−1, 1].

| Column | Type | Null | Description |
|---|---|---|---|
| ticker_a, ticker_b | VARCHAR | no | Pair, alphabetical order |
| trade_date | DATE | no | Last day of the window |
| window_label | VARCHAR | no | `126d` |
| correlation | DOUBLE | no | Correlation over the 126 returns ending at `trade_date` |
| computed_at | TIMESTAMP | no | Run time (UTC) |

No row when the window has a missing return for either ticker or zero dispersion (sample std ≤ 1e−12, `ZERO_DISPERSION`).

## Warehouse views

Created by `create_schema` with `CREATE OR REPLACE VIEW`.

### `price_adjustment_status`
One row per `price_adjustments` row, with the raw return on its `ex_date` and whether the
adjustment currently applies. Evaluated on every query, so a provider fix turns an adjustment
off after the next reload with no config change.

| Column | Type | Description |
|---|---|---|
| adjustment_id, ticker, ex_date, multiplier, apply_if_raw_return_above | | From `price_adjustments` |
| prev_date, prev_close | DATE, DOUBLE | Previous stored trading day of the ticker in `raw_prices` and its close (NULL if none) |
| ex_close | DOUBLE | `raw_prices` close on `ex_date` (NULL if the date is missing) |
| raw_return | DOUBLE | `ex_close / prev_close − 1` (NULL if either is missing) |
| applied | BOOLEAN | `raw_return > apply_if_raw_return_above`; false when `raw_return` is NULL |

### `prices_clean`
`raw_prices` with applied adjustments. **Read prices from here, never from `raw_prices`.**
Same rows and key as `raw_prices` (`(ticker, trade_date)`).

| Column | Type | Description |
|---|---|---|
| ticker, trade_date | VARCHAR, DATE | As in `raw_prices` |
| open, high, low, close | DOUBLE | Raw value × `adjustment_factor` |
| volume | BIGINT | As in `raw_prices`, **never corrected** (ADR 0003: the provider already rescaled pre-ex-date XLF volume with a different, undocumented factor; no metric uses volume) |
| source, ingested_at | VARCHAR, TIMESTAMP | As in `raw_prices` |
| adjustment_factor | DOUBLE | Product of the multipliers of applied adjustments whose `ex_date` is after `trade_date`; 1.0 when none |
| adjustment_ids | VARCHAR | Comma-separated ids of those adjustments (ordered by `ex_date`); NULL when none |

Today one adjustment applies: XLF ×1.18729 on 2444 rows (2007-01-04..2016-09-16). The XLF
return on 2016-09-19 is +19.56% in `raw_prices` and +0.70% in `prices_clean`.

### `macro_asof`
Point-in-time macro values on the trading calendar. **Read macro from here, never from
`raw_macro`.** Long format, one row per `(trade_date, series_id)` for every series of
`macro_publication_lags` (today all 9 of `config/macro.yaml`). Reads `raw_macro` directly
(documented exception, see "Data layers"). Created by `create_schema`; its series and lags come
from the tables synced by `mrm.storage.clean.prepare_clean_prices(con)`, so call that first
(without a sync the view is empty). Decision: `docs/decisions/0005-macro-publication-lags.md`.

| Column | Type | Description |
|---|---|---|
| trade_date | DATE | Trading day: every distinct `trade_date` in `prices_clean` (NYSE calendar as stored) |
| series_id | VARCHAR | A series of `macro_publication_lags` |
| value | DOUBLE | Value of the latest non-missing observation with `available_date < trade_date`; NULL if none exists |
| value_date | DATE | `obs_date` (reference date) of the value used; NULL when `value` is NULL |
| is_filled | BOOLEAN | `true` when the value was already available on the previous trading day (`available_date` before it), i.e. it is not new today. For the daily series (lag 0) this means a gap: a FRED `.` / NULL value or a missing row on the previous trading day. For monthly / weekly series it is `false` only on the first trading day a new release is usable. `false` when `value` is NULL |
| available_date | DATE | Release date applied to the value: `value_date + lag_days`, or its override, as a running max over `obs_date` (see rules); NULL when `value` is NULL |
| age_days | INTEGER | `trade_date − value_date` in calendar days: staleness of the value (grows when a release is missing); NULL when `value` is NULL |

Rules:
- **No look-ahead:** a value is usable only on trading days strictly after its release:
  `available_date < trade_date`, with `available_date = obs_date + lag_days` (or the override).
  Lag 0 gives the Phase 1.6 rule `obs_date < trade_date` for DGS10, DGS2, DGS3MO and T10Y2Y,
  whose rows are identical to Phase 1.6 (non-regression tests).
- **Latest reference period:** `available_date` is made non-decreasing in `obs_date` (running
  max): a delayed older release also delays newer ones, never advances them. Among values
  available on the same date the latest `obs_date` wins. Implemented as an `ASOF LEFT JOIN` on
  `trade_date > available_date`.
- **Forward-fill only forward:** missing values (FRED `.`, stored as NULL, or absent rows) are
  skipped and the latest earlier value is carried; `age_days` shows how old it is. Values are
  never filled backwards.
- **Series start:** on trading days before the first usable value of a series, `value`,
  `value_date`, `available_date` and `age_days` are NULL (no backfill).
- Observations on days the NYSE is closed are used on the next trading day; they never create
  rows.
- **Revisions are not handled:** values are FRED's latest vintage (see "Macro look-ahead risk").

| Series | Lag (days) | `obs_date` | Rows today | Max `age_days` today |
|---|---|---|---|---|
| DGS10, DGS2, DGS3MO, T10Y2Y | 0 | trading day | 5000 each | 4 |
| CPIAUCSL | 56 (+1 override) | 1st of the month | 5000 | 116 (2025-12-26, October 2025 never published) |
| UNRATE | 41 (+3) | 1st of the month | 5000 | 111 (2025-11-20, 2025 shutdown) |
| FEDFUNDS | 44 | 1st of the month | 5000 | 75 |
| NFCI | 6 (+4) | Friday ending the week | 5000 | 27 (2023-01-05) |
| STLFSI4 | 6 | Friday ending the week | 5000 | 13 |

## Data quality checks (`uv run mrm check`)

`mrm check` first prepares the clean layer (schema plus sync of
`config/price_adjustments.yaml` and `config/macro_lags.yaml`). The checks then read `prices_clean`, `raw_macro`,
`dim_asset` and `price_adjustment_status`. They never impute or modify data: exceptions in
the config only reclassify a finding (warning → info). An invalid
`config/data_quality.yaml`, `config/price_adjustments.yaml` or `config/macro_lags.yaml`
exits with code 2.
Thresholds and exceptions: `config/data_quality.yaml`. Code: `src/mrm/quality/`.
"Today" is the current date in New York. Exit code: `1` if any finding is critical, else `0`
(`2` for an invalid config). `--no-save` prints the report without writing `dq_results`.

| Check | Table | Severity | Rule (config key) |
|---|---|---|---|
| `price_duplicates` | prices_clean | critical | More than one row per `(ticker, trade_date)`. The PK prevents it today; the check guards against schema changes |
| `price_non_positive` | prices_clean | critical | `open`, `high`, `low` or `close` ≤ 0 |
| `price_missing_dates` | prices_clean | critical / warning | NYSE trading days missing between each ticker's **own** first and last date. A run of consecutive missing days longer than `prices.max_missing_gap_days` is critical, shorter runs are warnings |
| `price_non_trading_dates` | prices_clean | warning | Bars dated on a weekend, an NYSE holiday or a special closure |
| `price_extreme_returns` | prices_clean | warning / info | \|close/prev close − 1\| > `prices.extreme_return_abs`. Inside a `prices.known_extreme_returns` window it is info. Detail: count plus the `extreme_return_top_n` largest moves |
| `price_staleness` | dim_asset + prices_clean | critical | Last bar more than `prices.stale_after_business_days` NYSE trading days behind the **last trading day** (a Saturday run compares against Friday). A universe ticker with no bars is critical too |
| `price_adjustments` | price_adjustment_status | info / warning | Info when an adjustment applies. Warning when it is skipped: raw ex-date return ≤ `apply_if_raw_return_above` (the provider may have fixed its data; review and remove the yaml entry), or the ex-date / previous trading day is missing in `raw_prices` |
| `macro_freshness` | raw_macro | critical / warning | Last **non-missing** observation older than `macro.max_age_days[<frequency>]` calendar days (frequency from `macro.series_frequency`). A configured series with no data is critical. A stored series without a configured frequency is a warning |
| `macro_gaps` | raw_macro | info / warning | Each FRED missing value (`'.'`, stored as NULL) is classified; see below |
| `future_dates` | prices_clean + raw_macro | critical | `trade_date` or `obs_date` after today |

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

**Known finding (2026-11-18, resolved in Phase 1.5).** In `raw_prices`, XLF jumps +19.56% on
2016-09-19, the ex-date of the special distribution of XLRE shares (Real Estate spin-off).
Twelve Data's `adjust=all` mis-adjusts that distribution: the unadjusted price falls 18.25%,
in line with the USD 4.44356 distribution.
- It is corrected in `prices_clean` (×1.18729 before the ex-date, applied conditionally). The
  clean return on 2016-09-19 is +0.70%.
- No exception was added to `config/data_quality.yaml`. `mrm check` now reports
  `price_adjustments XLF` as info instead of the extreme-return warning.
- Derivation, sources, risks and how to revert: ADR 0003.

## Macro look-ahead risk

`raw_macro.obs_date` is FRED's **reference** date, not the date the value became public.
Joining `raw_macro` to prices on `obs_date` uses information **before** it was available
(look-ahead bias): never do it; read `macro_asof`, which applies the publication lag of each
series (ADR 0005):

| Series | `obs_date` | Released (first-release lag on ALFRED since 2005) | `lag_days` |
|---|---|---|---|
| CPIAUCSL | 1st of the reference month | mid following month, 8:30 ET (37–59 d) | 56 |
| UNRATE | 1st of the reference month | usually first Friday of the following month (31–80 d) | 41 |
| FEDFUNDS | 1st of the reference month | first business days of the following month (28–44 d) | 44 |
| NFCI | Friday ending the reference week | following Wednesday, Thursday after a holiday (5–20 d) | 6 |
| STLFSI4 | Friday ending the reference week | following week (5–6 d) | 6 |
| DGS10, DGS2, DGS3MO, T10Y2Y | Trading day | next business day | 0 |

Releases longer than `lag_days` are listed as overrides in `config/macro_lags.yaml`.
**Residual look-ahead:** FRED stores only the latest vintage, so revisions are not removed
(NFCI revisions are large: mean 0.147 against a standard deviation of 0.58), and NFCI before
2011-06 and STLFSI4 before 2022-11 are reconstructions. See `docs/methodology.md` and ADR 0005.

## CSV exports (`data/exports/`)

> TODO: document Tableau export schemas (later phases).
