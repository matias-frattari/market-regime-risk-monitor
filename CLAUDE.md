# Market Regime & Portfolio Risk Monitor

## Purpose
Data pipeline + analytics + Tableau dashboard that answers three questions for a long-term
investor in US-listed ETFs (Wall Street only):
1. What macro/market regime are we in? (expansion / slowdown / stress)
2. How risky is my portfolio, and where does the risk come from?
3. Would rebalancing help, and how do alternative allocation strategies behave per regime?

Audience of the repo: recruiters / hiring managers for remote data, BI and AI roles.
Everything must be reproducible, tested, documented and demoable in 2 minutes.

## Scope v1
IN: daily data ingestion, DuckDB warehouse, risk metrics, regime classification,
allocation strategies + backtest, CSV exports for Tableau Public, CI, README, docs.
OUT: real-time data, order execution, broker integrations, personal/real portfolio data,
investment advice. Include a disclaimer in README: educational project, not financial advice.

## Universe (config/universe.yaml, editable)
Core ETFs: SPY, QQQ, IWM (equity), EFA, EEM (intl equity), TLT, IEF, LQD (bonds), GLD (gold),
XLE, XLK, XLF, XLV (sectors). Cap the universe at ~15 tickers in v1 (free API limits).
Optional later: S&P 500 constituents.

## Data sources
- Prices (daily, adjusted): Twelve Data free plan (`/time_series`, `adjust=all`) behind a
  `PriceProvider` interface (decision: docs/decisions/0001-price-provider.md).
  Limit 8 req/min: space calls ~8 s apart (~100 s for 13 tickers). Cache raw responses,
  make ingestion incremental and idempotent. Key: `TWELVEDATA_API_KEY`.
- Macro (FRED API, free key): DGS10, DGS2, T10Y2Y, DGS3MO, FEDFUNDS, CPIAUCSL, UNRATE,
  VIXCLS, BAMLH0A0HYM2 (high-yield spread).
- SEC EDGAR is NOT in v1 (future extension).
- Secrets only via `.env` (never committed). Provide `.env.example`.

## Architecture
ingest (Python) -> DuckDB warehouse -> metrics / regimes / portfolio modules -> `data/exports/*.csv`
-> Tableau Public dashboard. Tableau Public reads files only, so CSV exports are the contract.

Repo layout:
```
config/            universe.yaml, portfolio.example.yaml, regimes.yaml
src/mrm/ingest/    providers, FRED client, incremental loaders
src/mrm/storage/   duckdb schema + helpers
src/mrm/metrics/   returns, vol, drawdown, VaR/CVaR, correlations, risk contribution
src/mrm/regimes/   rules-based v1, unsupervised v2 (GMM/HMM), comparison
src/mrm/portfolio/ equal weight, min-variance / max-Sharpe, HRP, backtest engine
src/mrm/export/    builds the Tableau CSV marts
tests/             pytest, no network (fixtures / mocked responses)
data/              raw + warehouse.duckdb are gitignored; data/exports/ is committed
dashboards/tableau/ screenshots, workbook link, data dictionary
docs/              architecture.md, methodology.md, data_dictionary.md, decisions/ (ADRs)
.github/workflows/ ci.yml (lint+tests), daily_update.yml (scheduled refresh)
```

## Stack & conventions
- Python 3.12, `uv` for env/deps, `pyproject.toml`. DuckDB, pandas, numpy, scipy, scikit-learn.
  Add libraries only with a one-line justification in docs/decisions/.
- `ruff` (lint+format) and `pytest`. Type hints on public functions. Small pure functions.
- Code, comments, docstrings, commit messages, README and docs: ENGLISH.
- Conventional commits (feat:, fix:, docs:, test:, chore:). One logical change per commit.
- Never commit: `.env`, API keys, `*.duckdb`, raw API dumps, notebooks with outputs holding keys.

## Data model (DuckDB)
raw_prices, raw_macro, dim_asset, fact_returns, fact_risk_metrics, fact_regime,
fact_backtest_equity, fact_backtest_summary. Every table documented in docs/data_dictionary.md.

## Metric definitions (document formulas in docs/methodology.md)
Daily log/simple returns; annualized vol (252d); rolling 60d vol; max drawdown;
Sharpe (risk-free = DGS3MO); historical and parametric VaR/CVaR at 95%;
correlation matrix (rolling 126d); Euler risk contribution per asset.

## Regimes
v1 rules (config/regimes.yaml, thresholds explicit and tunable): yield curve (T10Y2Y),
VIX level, HY spread, unemployment trend -> Expansion / Slowdown / Stress.
v2: unsupervised model (GMM or HMM) and a comparison against v1. Report honestly where
they disagree. Avoid look-ahead bias: use only information available at each date.

## Allocation & backtest
Strategies: equal weight, min-variance, max-Sharpe (Markowitz), HRP.
Monthly rebalancing, transaction cost 5 bps, walk-forward estimation window, benchmark SPY
and 60/40 (SPY/IEF). Report performance per regime. State limitations (estimation error,
in-sample bias) in methodology.md.

## Tableau exports (data/exports/)
Small, tidy CSVs: dim_assets, fact_risk_metrics,
fact_correlations, fact_regime, fact_backtest_equity, fact_backtest_summary.
Never export price series or daily per-ticker returns; export only aggregated metrics and
portfolio backtest curves (licensing of redistributed data, see docs/decisions/0001).
Dashboard pages: 1) Macro Regime, 2) Portfolio Risk, 3) Rebalancing & Backtest.

## How Claude Code must work in this repo
1. For any non-trivial task: propose a short plan first, wait for approval.
2. Write tests with the code. Run `ruff` and `pytest` before declaring done.
3. Keep changes small and surgical; do not rewrite files that already work.
4. Never invent data or results. If an API call fails, report it; do not fabricate fixtures
   that pretend to be real market data (synthetic fixtures must be labeled as such).
5. Update docs/ and the Commands section below whenever behavior changes.
6. Ask before adding dependencies, changing the data model, or touching CI.

## Definition of done (project)
`git clone` + documented commands reproduce the warehouse and exports; CI green;
data refreshed on a schedule; README with problem, method, results, screenshots and a
live Tableau Public link; methodology and limitations documented.

## Commands (fill in as they are built)
- setup: TBD
- ingest: TBD
- test: TBD
- export: TBD
