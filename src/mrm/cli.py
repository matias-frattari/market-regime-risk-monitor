"""``mrm`` command: a thin dispatcher over the existing module entry points.

    uv run mrm ingest prices [--tickers SPY,TLT] [--dry-run]   (= python -m mrm.ingest.prices)
    uv run mrm ingest macro  [--series DGS10] [--dry-run]      (= python -m mrm.ingest.macro)
    uv run mrm check         [--no-save]                       (= python -m mrm.quality.run)
    uv run mrm metrics       [--portfolio PATH] [--dry-run]    (= python -m mrm.metrics.run)
    uv run mrm regimes       [--dry-run]                       (= python -m mrm.regimes.run)

Each subcommand forwards its remaining arguments unchanged; options and exit codes are the
ones of the target module. Imports are lazy so ``mrm check`` does not load the HTTP clients.
"""

import importlib
import sys
from collections.abc import Sequence

COMMANDS: dict[tuple[str, ...], str] = {
    ("ingest", "prices"): "mrm.ingest.prices",
    ("ingest", "macro"): "mrm.ingest.macro",
    ("check",): "mrm.quality.run",
    ("metrics",): "mrm.metrics.run",
    ("regimes",): "mrm.regimes.run",
}
USAGE = (
    "usage: mrm {ingest prices | ingest macro | check | metrics | regimes} [options]"
    "  (add --help for options)"
)


def main(argv: Sequence[str] | None = None) -> int:
    """Dispatch to the target module's ``main(argv)``. Exit code 2 on an unknown command."""
    args = list(sys.argv[1:] if argv is None else argv)
    for words, module in COMMANDS.items():
        if tuple(args[: len(words)]) == words:
            return importlib.import_module(module).main(args[len(words) :])
    print(USAGE)
    return 0 if args[:1] in (["-h"], ["--help"]) else 2


if __name__ == "__main__":
    sys.exit(main())
