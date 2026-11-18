"""Console report and exit code of a data quality run."""

from collections import Counter
from collections.abc import Sequence
from datetime import date

from mrm.quality.checks import CRITICAL, INFO, WARNING
from mrm.storage.models import Finding

SEVERITY_ORDER: tuple[str, ...] = (CRITICAL, WARNING, INFO)
RUN_CHECK = "run"  # check_name of the per-run summary row in dq_results


def severity_counts(findings: Sequence[Finding]) -> Counter[str]:
    """Number of findings per severity (summary rows excluded)."""
    return Counter(f.severity for f in findings if f.check != RUN_CHECK)


def summary_finding(findings: Sequence[Finding], checks_run: int) -> Finding:
    """Info row stored with every run, so clean runs are recorded too."""
    counts = severity_counts(findings)
    detail = f"{checks_run} checks; " + ", ".join(f"{s}={counts[s]}" for s in SEVERITY_ORDER)
    return Finding(RUN_CHECK, "all", INFO, sum(counts.values()), detail)


def exit_code(findings: Sequence[Finding]) -> int:
    """1 when any finding is critical, else 0 (warnings and info never fail the run)."""
    return 1 if severity_counts(findings)[CRITICAL] else 0


def format_report(findings: Sequence[Finding], run_id: str, today: date) -> str:
    """Findings grouped by severity. Only ids, dates and counts: never environment values."""
    lines = [f"Data quality report  run={run_id}  as of {today}", ""]
    for severity in SEVERITY_ORDER:
        group = sorted(
            (f for f in findings if f.severity == severity and f.check != RUN_CHECK),
            key=lambda f: (f.check, f.entity),
        )
        lines.append(f"{severity.upper()} ({len(group)})")
        if not group:
            lines.append("  none")
        width_check = max((len(f.check) for f in group), default=0)
        width_entity = max((len(f.entity) for f in group), default=0)
        for f in group:
            lines.append(
                f"  {f.check.ljust(width_check)}  {f.entity.ljust(width_entity)}  "
                f"n={f.n_affected:<5}  {f.detail}"
            )
        lines.append("")
    counts = severity_counts(findings)
    status = "FAIL" if counts[CRITICAL] else "PASS"
    lines.append(
        f"{status}: critical={counts[CRITICAL]} warning={counts[WARNING]} info={counts[INFO]}"
    )
    return "\n".join(lines)
