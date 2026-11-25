from pathlib import Path

import pytest
import yaml

from mrm.config import REGIME_MODEL_PATH, load_regime_model

SERIES = ("NFCI", "T10Y2Y", "STLFSI4", "VIXCLS")


def _base() -> dict:
    return yaml.safe_load(REGIME_MODEL_PATH.read_text(encoding="utf-8"))


def _write(tmp_path: Path, raw: dict) -> Path:
    path = tmp_path / "regime_model.yaml"
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    return path


def test_repo_config_loads_the_documented_values() -> None:
    cfg = load_regime_model()
    assert (cfg.n_states, cfg.covariance_type) == (3, "diag")
    assert (cfg.n_init, cfg.seed, cfg.max_iter, cfg.tol) == (10, 0, 200, 1e-4)
    assert (cfg.vol_ticker, cfg.vol_window) == ("SPY", 60)
    assert cfg.macro_features == ("NFCI", "T10Y2Y")
    assert cfg.zscore_min_obs == 60
    assert (cfg.min_train_days, cfg.refit_every_days) == (756, 252)
    assert [w.name for w in cfg.windows] == [
        "GFC", "COVID", "2022 rate shock", "March-2023 bank stress",
    ]  # fmt: skip
    assert all(w.source for w in cfg.windows)


def test_point_in_time_warmup_is_derived_from_the_config() -> None:
    cfg = load_regime_model()
    assert cfg.point_in_time_warmup == (
        cfg.vol_window + cfg.zscore_min_obs - 1 + cfg.min_train_days
    )


@pytest.mark.parametrize(
    ("section", "key", "value"),
    [
        ("model", "n_states", 2),
        ("model", "covariance_type", "full"),
        ("model", "type", "gmm"),
        ("model", "order_states_by", "nfci"),
        ("features", "standardization", "full_sample"),
        ("features", "macro", ["NFCI"]),
        ("features", "macro", ["T10Y2Y", "NFCI"]),
    ],
)
def test_fixed_values_are_enforced(tmp_path: Path, section: str, key: str, value: object) -> None:
    raw = _base()
    raw[section][key] = value
    with pytest.raises(ValueError, match="must be"):
        load_regime_model(_write(tmp_path, raw), series_ids=SERIES)


@pytest.mark.parametrize(("key", "value"), [("ticker", "QQQ"), ("window", 20)])
def test_vol_feature_is_fixed(tmp_path: Path, key: str, value: object) -> None:
    raw = _base()
    raw["features"]["spy_vol"][key] = value
    with pytest.raises(ValueError, match=f"spy_vol.{key}"):
        load_regime_model(_write(tmp_path, raw), series_ids=SERIES)


def test_copyrighted_series_rejected(tmp_path: Path) -> None:
    raw = _base()
    raw["features"]["macro"] = ["NFCI", "VIXCLS"]
    with pytest.raises(ValueError, match="copyrighted"):
        load_regime_model(_write(tmp_path, raw), series_ids=SERIES)


def test_unknown_series_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="macro_lags"):
        load_regime_model(_write(tmp_path, _base()), series_ids=("NFCI",))


@pytest.mark.parametrize(
    ("section", "key", "value"),
    [
        ("model", "n_init", 0),
        ("model", "seed", -1),
        ("model", "max_iter", 0),
        ("model", "tol", 0),
        ("features", "zscore_min_obs", 1),
        ("point_in_time", "min_train_days", 0),
        ("point_in_time", "refit_every_days", True),
    ],
)
def test_counts_validated(tmp_path: Path, section: str, key: str, value: object) -> None:
    raw = _base()
    raw[section][key] = value
    with pytest.raises(ValueError, match=key):
        load_regime_model(_write(tmp_path, raw), series_ids=SERIES)


def test_window_needs_a_source_and_order(tmp_path: Path) -> None:
    raw = _base()
    del raw["report"]["windows"][0]["source"]
    with pytest.raises(ValueError, match="source"):
        load_regime_model(_write(tmp_path, raw), series_ids=SERIES)
    raw = _base()
    w = raw["report"]["windows"][1]
    w["start"], w["end"] = w["end"], w["start"]
    with pytest.raises(ValueError, match="ends before"):
        load_regime_model(_write(tmp_path, raw), series_ids=SERIES)
