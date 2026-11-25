# Market Regime & Portfolio Risk Monitor

> **Disclaimer:** This project is for educational purposes only. Nothing here constitutes
> financial advice. Do not make investment decisions based on this code or its outputs.

A data pipeline and analytics dashboard that answers three questions for a long-term
investor in US-listed ETFs:

1. What macro/market regime are we in? (expansion / slowdown / stress)
2. How risky is my portfolio, and where does the risk come from?
3. Would rebalancing help, and how do alternative allocation strategies compare per regime?

Built with Python, DuckDB, and Tableau Public. Fully reproducible and CI-tested.

---

## Quick start

```bash
# 1. Clone and enter the repo
git clone <repo-url>
cd market-regime-risk-monitor

# 2. Copy secrets template and fill in your API keys
cp .env.example .env

# 3. Install dependencies (requires uv: https://docs.astral.sh/uv/)
uv sync

# 4. Run checks
uv run ruff check .
uv run pytest

# 5. Commands (populated as phases are built)
# setup:   TBD
# ingest:  uv run python -m mrm.ingest.prices [--tickers SPY,TLT] [--dry-run]
# metrics: uv run mrm metrics [--portfolio PATH] [--dry-run]
# regimes: uv run mrm regimes [--dry-run]
# test:    uv run pytest
# export:  TBD
```

---

## Project layout

```
config/            universe.yaml, portfolio.example.yaml, regime_rules.yaml
src/mrm/ingest/    price providers, FRED client, incremental loaders
src/mrm/storage/   DuckDB schema + helpers
src/mrm/metrics/   returns, vol, drawdown, VaR/CVaR, correlations, risk contribution
src/mrm/regimes/   rules-based v1, unsupervised v2 (GMM/HMM), comparison
src/mrm/portfolio/ equal weight, min-variance, max-Sharpe, HRP, backtest engine
src/mrm/export/    builds the Tableau CSV marts
tests/             pytest, no network (fixtures / mocked responses)
data/exports/      tidy CSVs for Tableau Public (committed)
dashboards/tableau/ screenshots, workbook link, data dictionary
docs/              architecture.md, methodology.md, data_dictionary.md, decisions/ (ADRs)
.github/workflows/ ci.yml (lint + tests), daily_update.yml (scheduled refresh)
```

---

## Tableau dashboard

> Link: _coming soon_

---

## Data sources

- **Prices (daily, adjusted):** one provider behind a `PriceProvider` interface (decided in step 0.2)
- **Macro:** FRED API (free) — Treasury yields and yield curve, fed funds, CPI, unemployment,
  Fed financial stress indices (NFCI, STLFSI4). VIX and HY spread are excluded (copyrighted,
  see [ADR 0002](docs/decisions/0002-macro-series-licensing.md)).

---

## License

MIT. See [LICENSE](LICENSE).
