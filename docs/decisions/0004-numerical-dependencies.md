# ADR 0004: numpy and pandas for the metrics layer

- **Status:** Accepted
- **Date:** 2026-11-24

## Context

Phase 2 computes returns, rolling volatility, drawdowns, VaR/CVaR, rolling correlations and
Euler risk contributions over a 13-ticker x ~5000-day panel. The project rule is to add a
library only with a one-line justification.

## Decision

- **numpy**: vectorised linear algebra (covariance, `w' Σ w`, Euler contributions) and
  quantiles; also required by DuckDB's `fetchnumpy`/`df` result conversion.
- **pandas**: date-indexed panels with explicit NaN alignment and rolling windows
  (`rolling(window, min_periods=window)`), and DuckDB `con.register` / `.df()` I/O.
- **Not scipy**: the only distribution function needed (normal quantile and density for
  parametric VaR/CVaR) comes from the standard library `statistics.NormalDist`.

## Consequences

Two runtime dependencies (plus their transitive `python-dateutil`, `tzdata`), pinned in
`uv.lock`. Calculation functions take and return pandas/numpy objects; no database access
inside them.
