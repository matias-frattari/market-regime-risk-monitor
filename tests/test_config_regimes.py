from datetime import date
from pathlib import Path

import pytest
import yaml

from mrm.config import REGIME_RULES_PATH, REGIME_SIGNALS, load_regime_rules

SERIES = ("NFCI", "STLFSI4", "T10Y2Y", "UNRATE", "DGS10")


def _base() -> dict:
    return yaml.safe_load(REGIME_RULES_PATH.read_text(encoding="utf-8"))


def _write(tmp_path: Path, raw: dict) -> Path:
    path = tmp_path / "regime_rules.yaml"
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    return path


def test_repo_config_loads_the_documented_values() -> None:
    rules = load_regime_rules()
    assert (rules.regime.signal, rules.regime.tension_at, rules.regime.stress_at) == (
        "NFCI", 0.0, 1.0,
    )  # fmt: skip
    assert rules.robustness.signal == "STLFSI4"
    assert rules.regime.first_vintage == date(2011, 6, 2)
    assert rules.robustness.first_vintage == date(2022, 11, 17)
    assert "ADR" in rules.regime.vintage_source or "0005" in rules.regime.vintage_source
    assert (rules.curve_signal, rules.curve_below) == ("T10Y2Y", 0.0)
    assert rules.sahm.threshold_pp == 0.5
    assert (rules.sahm.ma_months, rules.sahm.lookback_months) == (3, 12)
    assert rules.nfci_stale_days == 14
    assert [e.name for e in rules.known_events] == ["2025 federal shutdown"]


def test_signals_are_in_macro_lags() -> None:
    from mrm.config import load_macro_lags

    known = {lag.series_id for lag in load_macro_lags()}
    assert set(REGIME_SIGNALS.values()) <= known


def test_stress_must_exceed_tension(tmp_path: Path) -> None:
    raw = _base()
    raw["regime"]["stress_at"] = raw["regime"]["tension_at"]
    with pytest.raises(ValueError, match="stress_at must be greater"):
        load_regime_rules(_write(tmp_path, raw), SERIES)


@pytest.mark.parametrize("signal", ["VIXCLS", "BAMLH0A0HYM2"])
def test_copyrighted_signal_rejected(tmp_path: Path, signal: str) -> None:
    raw = _base()
    raw["regime"]["signal"] = signal
    with pytest.raises(ValueError, match="copyrighted"):
        load_regime_rules(_write(tmp_path, raw), SERIES)


def test_unknown_signal_rejected(tmp_path: Path) -> None:
    raw = _base()
    raw["flags"]["curve_inverted"]["signal"] = "SYN_CURVE"
    with pytest.raises(ValueError, match="not in config/macro_lags.yaml"):
        load_regime_rules(_write(tmp_path, raw), SERIES)


def test_signal_cannot_be_swapped(tmp_path: Path) -> None:
    raw = _base()
    raw["regime"]["signal"] = "DGS10"
    with pytest.raises(ValueError, match="must be NFCI"):
        load_regime_rules(_write(tmp_path, raw), SERIES)


@pytest.mark.parametrize("key", ["regime", "robustness", "flags", "vintages", "report"])
def test_missing_section_rejected(tmp_path: Path, key: str) -> None:
    raw = _base()
    del raw[key]
    with pytest.raises(ValueError, match=f"'{key}' mapping"):
        load_regime_rules(_write(tmp_path, raw), SERIES)


def test_missing_vintage_source_rejected(tmp_path: Path) -> None:
    raw = _base()
    del raw["vintages"]["NFCI"]["source"]
    with pytest.raises(ValueError, match="needs a 'source'"):
        load_regime_rules(_write(tmp_path, raw), SERIES)


def test_min_obs_cannot_exceed_window(tmp_path: Path) -> None:
    raw = _base()
    raw["flags"]["unemployment_rising"]["ma_min_obs"] = 4
    with pytest.raises(ValueError, match="cannot exceed"):
        load_regime_rules(_write(tmp_path, raw), SERIES)


def test_non_numeric_threshold_rejected(tmp_path: Path) -> None:
    raw = _base()
    raw["regime"]["tension_at"] = "zero"
    with pytest.raises(ValueError, match="finite number"):
        load_regime_rules(_write(tmp_path, raw), SERIES)


def test_event_ending_before_start_rejected(tmp_path: Path) -> None:
    raw = _base()
    raw["report"]["known_events"][0]["end"] = date(2025, 9, 1)
    with pytest.raises(ValueError, match="ends before it starts"):
        load_regime_rules(_write(tmp_path, raw), SERIES)
