"""Classify each trading day into a rules-based regime and write ``fact_regimes``.

CLI::

    uv run mrm regimes [--dry-run]

Prepares the clean layer (``prepare_clean_prices``: without it ``macro_asof`` is empty), reads
NFCI, STLFSI4, T10Y2Y and UNRATE from ``macro_asof`` on the NYSE trading days of
``prices_clean``, applies ``config/regime_rules.yaml`` (ADR 0006) and rebuilds
``fact_regimes`` in one transaction. Prints only aggregates (shares, counts, dates).

Exit codes: 0 success, 1 no usable data, 2 config error.
"""

import argparse
import sys
from collections.abc import Sequence
from datetime import UTC, datetime

import duckdb

from mrm.config import (
    load_data_quality_config,
    load_macro_lags,
    load_price_adjustments,
    load_regime_rules,
)
from mrm.regimes.io import load_regime_inputs, read_regimes, write_regimes
from mrm.regimes.report import format_report
from mrm.regimes.rules import build_regimes
from mrm.storage.clean import prepare_clean_prices
from mrm.storage.db import connect
from mrm.storage.models import PriceAdjustment, RegimeRules


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="mrm regimes",
        description="Rules-based regimes (config/regime_rules.yaml) into fact_regimes.",
    )
    parser.add_argument("--dry-run", action="store_true", help="Compute but write nothing")
    return parser.parse_args(argv)


def main(
    argv: Sequence[str] | None = None,
    *,
    con: duckdb.DuckDBPyConnection | None = None,
    rules: RegimeRules | None = None,
    adjustments: Sequence[PriceAdjustment] | None = None,
) -> int:
    """CLI entry point. ``con``/``rules``/``adjustments`` are injectable for tests."""
    args = _parse_args(argv)
    try:
        rules = rules or load_regime_rules()
        adjustments = load_price_adjustments() if adjustments is None else adjustments
        closures = load_data_quality_config().special_closures
        macro_lags = load_macro_lags()
    except (OSError, ValueError) as exc:
        print(f"error: invalid config: {exc}")
        return 2

    signals = [rules.regime.signal, rules.robustness.signal, rules.curve_signal, rules.sahm.signal]
    own_con = con is None
    con = con or connect()
    try:
        prepare_clean_prices(con, adjustments=adjustments, macro_lags=macro_lags)
        inputs, n_off_calendar = load_regime_inputs(con, signals, closures)
        if inputs[rules.regime.signal].empty:
            print("error: macro_asof has no trading days; run `mrm ingest prices` first")
            return 1
        regimes = build_regimes(inputs, rules)
        if not args.dry_run:
            write_regimes(con, regimes, datetime.now(UTC).replace(tzinfo=None))
            regimes = read_regimes(con)
    finally:
        if own_con:
            con.close()

    lo, hi = regimes["trade_date"].min(), regimes["trade_date"].max()
    print(
        f"fact_regimes: {len(regimes)} trading days, {lo:%Y-%m-%d} .. {hi:%Y-%m-%d} "
        f"(one row per day: {regimes['trade_date'].is_unique}); "
        f"off-calendar days discarded: {n_off_calendar}"
    )
    for line in format_report(regimes, rules):
        print(line)
    print("")
    print("dry run: nothing written" if args.dry_run else "written (full rebuild, one transaction)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
