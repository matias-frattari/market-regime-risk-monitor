"""Fit the HMM regimes v2 and write ``fact_regimes_v2``; compare with v1.

CLI::

    uv run mrm regimes-v2 [--dry-run]

Prepares the clean layer (``prepare_clean_prices``), reads the SPY close from ``prices_clean``
and NFCI / T10Y2Y (plus the v1 inputs) from ``macro_asof`` on NYSE trading days, builds the
point-in-time and full-sample variants (``config/regime_model.yaml``, ADR 0007), recomputes v1
in memory with ``config/regime_rules.yaml`` (fact_regimes is not written) and rebuilds
``fact_regimes_v2`` in one transaction. Prints only aggregates.

Exit codes: 0 success, 1 no usable data (or too short for a first fit), 2 config error.
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
    load_regime_model,
    load_regime_rules,
)
from mrm.metrics.io import load_close_panel
from mrm.regimes.features import build_features
from mrm.regimes.io import load_regime_inputs, read_regimes_v2, write_regimes_v2
from mrm.regimes.report_v2 import format_report_v2
from mrm.regimes.rules import build_regimes
from mrm.regimes.v2 import build_regimes_v2
from mrm.storage.clean import prepare_clean_prices
from mrm.storage.db import connect
from mrm.storage.models import PriceAdjustment, RegimeModelConfig, RegimeRules


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="mrm regimes-v2",
        description="HMM regimes v2 (config/regime_model.yaml) into fact_regimes_v2.",
    )
    parser.add_argument("--dry-run", action="store_true", help="Compute but write nothing")
    return parser.parse_args(argv)


def main(
    argv: Sequence[str] | None = None,
    *,
    con: duckdb.DuckDBPyConnection | None = None,
    model: RegimeModelConfig | None = None,
    rules: RegimeRules | None = None,
    adjustments: Sequence[PriceAdjustment] | None = None,
) -> int:
    """CLI entry point. ``con``/``model``/``rules``/``adjustments`` are injectable for tests."""
    args = _parse_args(argv)
    try:
        model = model or load_regime_model()
        rules = rules or load_regime_rules()
        adjustments = load_price_adjustments() if adjustments is None else adjustments
        closures = load_data_quality_config().special_closures
        macro_lags = load_macro_lags()
    except (OSError, ValueError) as exc:
        print(f"error: invalid config: {exc}")
        return 2

    v1_signals = [rules.regime.signal, rules.robustness.signal, rules.curve_signal]
    signals = list(dict.fromkeys([*v1_signals, rules.sahm.signal, *model.macro_features]))
    own_con = con is None
    con = con or connect()
    try:
        prepare_clean_prices(con, adjustments=adjustments, macro_lags=macro_lags)
        inputs, n_off_calendar = load_regime_inputs(con, signals, closures)
        days = inputs[rules.regime.signal].index
        panel, _ = load_close_panel(con, closures)
        if days.empty or model.vol_ticker not in panel.columns:
            print(f"error: no trading days or no {model.vol_ticker} prices in prices_clean; "
                  "run `mrm ingest prices` first")  # fmt: skip
            return 1
        spy_close = panel[model.vol_ticker].reindex(days)
        macro = {s: inputs[s]["value"] for s in model.macro_features}
        features = build_features(spy_close, macro, model.vol_window)
        result = build_regimes_v2(features, model)
        if not any(r.variant == "point_in_time" for r in result.refits):
            print(
                f"error: fewer than {model.min_train_days} valid rows before the end of the "
                f"data ({len(days)} trading days): no point-in-time fit possible"
            )
            return 1
        v1 = build_regimes(inputs, rules)
        frame = result.frame
        if not args.dry_run:
            write_regimes_v2(con, frame, datetime.now(UTC).replace(tzinfo=None))
            frame = read_regimes_v2(con)
    finally:
        if own_con:
            con.close()

    lo, hi = frame["trade_date"].min(), frame["trade_date"].max()
    per_variant = frame.groupby("variant")["trade_date"].agg(["size", "nunique"])
    one_row = bool((per_variant["size"] == per_variant["nunique"]).all())
    counts = ", ".join(f"{v} {int(n)}" for v, n in per_variant["size"].items())
    print(
        f"fact_regimes_v2: {len(frame)} rows ({counts}), {lo:%Y-%m-%d} .. {hi:%Y-%m-%d} "
        f"(one row per day per variant: {one_row}); "
        f"{model.vol_ticker} missing closes: {int(spy_close.isna().sum())}; "
        f"off-calendar days discarded: {n_off_calendar}"
    )
    for line in format_report_v2(frame, result.refits, result.prediction_only_rows, v1, model):
        print(line)
    print("")
    print("dry run: nothing written" if args.dry_run else "written (full rebuild, one transaction)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
