import types

import pytest

from mrm import cli


@pytest.fixture
def calls(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, list[str]]]:
    """Replace each target module with a stub that records its argv (no network, no DB)."""
    recorded: list[tuple[str, list[str]]] = []

    def fake_import(name: str) -> types.SimpleNamespace:
        def main(argv: list[str]) -> int:
            recorded.append((name, argv))
            return 7

        return types.SimpleNamespace(main=main)

    monkeypatch.setattr(cli.importlib, "import_module", fake_import)
    return recorded


@pytest.mark.parametrize(
    ("argv", "module", "rest"),
    [
        (["ingest", "prices", "--tickers", "SPY", "--dry-run"], "mrm.ingest.prices",
         ["--tickers", "SPY", "--dry-run"]),
        (["ingest", "macro", "--series", "NFCI"], "mrm.ingest.macro", ["--series", "NFCI"]),
        (["check"], "mrm.quality.run", []),
        (["check", "--no-save"], "mrm.quality.run", ["--no-save"]),
        (["metrics", "--dry-run"], "mrm.metrics.run", ["--dry-run"]),
    ],
)  # fmt: skip
def test_dispatches_to_module_main(
    calls: list[tuple[str, list[str]]], argv: list[str], module: str, rest: list[str]
) -> None:
    assert cli.main(argv) == 7  # the target's exit code is returned unchanged
    assert calls == [(module, rest)]


@pytest.mark.parametrize("argv", [[], ["ingest"], ["ingest", "fx"], ["export"]])
def test_unknown_command_exits_2(
    calls: list[tuple[str, list[str]]], argv: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli.main(argv) == 2
    assert calls == []
    assert "usage: mrm" in capsys.readouterr().out


def test_help_exits_0(calls: list[tuple[str, list[str]]]) -> None:
    assert cli.main(["--help"]) == 0
