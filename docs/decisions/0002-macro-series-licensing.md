# ADR 0002: Macro series and licensing (FRED)

- **Status:** Accepted
- **Date:** 2026-11-17

## Context

The regime model needs macro and market-stress inputs from FRED. The original brief listed
VIXCLS (Cboe VIX) and BAMLH0A0HYM2 (ICE BofA US High Yield OAS). Several FRED series belong to
third parties, and FRED marks them with copyright notes. This project publishes derived data
(Tableau Public, committed CSVs), so every series it depends on must be redistributable and
have history back to at least 2007, which covers the global financial crisis.

## Evidence

Metadata from the FRED `fred/series` endpoint, retrieved 2026-11-17 with a temporary,
untracked script; no key or raw response stored. "Copyright" and "permission" mean the
series `notes` field contains that word.

| id | title | freq | observation_start | observation_end | copyright | permission |
|---|---|---|---|---|---|---|
| DGS10 | 10-Year Treasury CMT | D | 1962-01-02 | 2026-11-16 | no | no |
| DGS2 | 2-Year Treasury CMT | D | 1976-06-01 | 2026-11-16 | no | no |
| T10Y2Y | 10Y minus 2Y CMT | D | 1976-06-01 | 2026-11-17 | no | no |
| DGS3MO | 3-Month Treasury CMT | D | 1981-09-01 | 2026-11-16 | no | no |
| FEDFUNDS | Federal Funds Effective Rate | M | 1954-07-01 | 2026-10-01 | no | no |
| CPIAUCSL | CPI-U All Items (SA) | M | 1947-01-01 | 2026-10-01 | no | no |
| UNRATE | Unemployment Rate (SA) | M | 1948-01-01 | 2026-10-01 | no | no |
| VIXCLS | CBOE Volatility Index: VIX | D | 1990-01-02 | 2026-11-16 | **yes** | **yes** |
| BAMLH0A0HYM2 | ICE BofA US High Yield Index OAS | D | 2023-11-20 | 2026-11-16 | **yes** | **yes** |
| NFCI | Chicago Fed National Financial Conditions Index | W | 1971-01-08 | 2026-11-06 | no | no |
| STLFSI4 | St. Louis Fed Financial Stress Index | W | 1993-12-31 | 2026-11-06 | no | no |

## Decision

1. **Excluded:** VIXCLS and BAMLH0A0HYM2. Both are copyrighted. BAMLH0A0HYM2 also has only
   3 years of history on FRED. They are not ingested, published, or used by the regime
   model. `load_macro_series` rejects them (`BLOCKED_SERIES` in `src/mrm/config.py`).
2. **Replacements:**
   - **NFCI** is the primary financial-stress signal for regimes. It is public, has the
     longest history, and 0 means average conditions.
   - **STLFSI4** is ingested as a robustness check and for the v1/v2 comparison.
   - The **realized volatility of SPY** (60d, computed in Phase 2 from licensed prices that
     are never exported) replaces the VIX level.
3. **Ingested series:** DGS10, DGS2, T10Y2Y, DGS3MO, FEDFUNDS, CPIAUCSL, UNRATE, NFCI,
   STLFSI4 (`config/macro.yaml`).
4. **Reload rule:**
   - NFCI and STLFSI4 are factor-model estimates whose whole past can be revised every week.
     They are re-fetched in full on every run (`overlap_days: full`, about 3k rows each,
     one request), then upserted.
   - All other series use the 90-day overlap window.

## Consequences

- Realized volatility is backward-looking, while the VIX is forward-looking (implied).
  Stress regimes may therefore be detected somewhat later.
- NFCI includes credit-spread information, which partly replaces the excluded HY spread.
- Weekly and monthly series have publication lags. The regime module must lag them so it uses
  only values published by each date (see `config/regime_rules.yaml`, renamed from
  `config/regimes.yaml` in step 3.1; lags in ADR 0005).
- FRED only serves the latest vintage. Revised values (including the full-history revisions
  of NFCI and STLFSI4) are a residual look-ahead bias, documented in `docs/methodology.md`.
- Any series added to `config/macro.yaml` must first pass the same metadata check.
