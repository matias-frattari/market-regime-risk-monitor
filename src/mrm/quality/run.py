"""Run all data quality checks, store the findings in ``dq_results`` and print a report.

CLI::

    uv run mrm check [--no-save]

Price checks read ``prices_clean``; the clean layer is prepared (schema + sync of
``config/price_adjustments.yaml``) before the checks run.

Exit codes: 0 no critical findings, 1 at least one critical finding, 2 config error.
"""

import argparse
import sys
import uuid
from collections.abc import Sequence
from datetime import date, datetime
from zoneinfo import ZoneInfo

import duckdb

from mrm.config import (
    DataQualityConfig,
    load_data_quality_config,
    load_macro_lags,
    load_price_adjustments,
)
from mrm.quality.checks import ALL_CHECKS, run_checks
from mrm.quality.report import exit_code, format_report, summary_finding
from mrm.storage.clean import prepare_clean_prices
from mrm.storage.db import connect
from mrm.storage.loaders import insert_dq_results
from mrm.storage.models import PriceAdjustment

NEW_YORK = ZoneInfo("America/New_York")


def new_york_today() -> date:
    """Today's date on the US market clock."""
    return datetime.now(NEW_YORK).date()


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="mrm check",
        description="Data quality checks on prices_clean and raw_macro (config/data_quality.yaml).",
    )
    parser.add_argument(
        "--no-save", action="store_true", help="Print the report but do not write dq_results"
    )
    return parser.parse_args(argv)


def main(
    argv: Sequence[str] | None = None,
    *,
    con: duckdb.DuckDBPyConnection | None = None,
    cfg: DataQualityConfig | None = None,
    today: date | None = None,
    adjustments: Sequence[PriceAdjustment] | None = None,
) -> int:
    """CLI entry point. ``con``/``cfg``/``today``/``adjustments`` are injectable for tests."""
    args = _parse_args(argv)
    try:
        cfg = cfg or load_data_quality_config()
    except (OSError, ValueError) as exc:
        print(f"error: invalid config/data_quality.yaml: {exc}")
        return 2
    try:
        adjustments = load_price_adjustments() if adjustments is None else adjustments
    except (OSError, ValueError) as exc:
        print(f"error: invalid config/price_adjustments.yaml: {exc}")
        return 2
    try:
        macro_lags = load_macro_lags()
    except (OSError, ValueError) as exc:
        print(f"error: invalid config/macro_lags.yaml: {exc}")
        return 2
    today = today or new_york_today()
    run_id = uuid.uuid4().hex[:12]

    own_con = con is None
    con = con or connect()
    try:
        prepare_clean_prices(con, adjustments=adjustments, macro_lags=macro_lags)
        findings = run_checks(con, cfg, today)
        findings.append(summary_finding(findings, len(ALL_CHECKS)))
        if not args.no_save:
            insert_dq_results(con, run_id, findings)
    finally:
        if own_con:
            con.close()

    print(format_report(findings, run_id, today))
    if not args.no_save:
        print(f"Saved {len(findings)} row(s) to dq_results (run_id={run_id})")
    return exit_code(findings)


if __name__ == "__main__":
    sys.exit(main())
