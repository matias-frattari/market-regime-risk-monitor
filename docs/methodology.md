# Methodology

> TODO: document metric definitions and regime classification logic (Phase 1+)

## Macro data vintages (limitation)

Macro series come from FRED, which serves only the **latest vintage** of each series.
Revisions are applied retroactively. A backtest that reads a past date therefore sees the
revised value, not the value that was known at the time. The full-history revisions of
NFCI and STLFSI4 are an example. This is a residual look-ahead bias. Real-time vintages
(ALFRED) are out of scope for v1. Publication lags (weekly indices released the following
week, monthly series the next month) are handled in the regime module. See
`docs/decisions/0002-macro-series-licensing.md`.
