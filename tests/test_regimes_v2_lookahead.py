# SYNTHETIC — not real market data (see tests/regimes_v2_synthetic.py).
"""Non-regression: the point-in-time regimes v2 never use information after each trading day."""

import numpy as np
import pandas as pd
import pytest

from mrm.regimes.features import expanding_zscore
from mrm.regimes.hmm import forward_filter, smoothed
from mrm.regimes.v2 import (
    FULL_SAMPLE,
    POINT_IN_TIME,
    PROB_COLUMNS,
    _fit,
    build_full_sample,
    build_point_in_time,
    build_regimes_v2,
    refit_positions,
)
from tests.regimes_v2_synthetic import SMALL_CFG, synthetic_features

FEATURES = synthetic_features()
FULL, REFITS, PREDICTION_ONLY = build_point_in_time(FEATURES, SMALL_CFG)
R = refit_positions(
    expanding_zscore(FEATURES, SMALL_CFG.zscore_min_obs).notna().all(axis=1).to_numpy(),
    SMALL_CFG.min_train_days,
    SMALL_CFG.refit_every_days,
)


def test_synthetic_setup_has_several_refits() -> None:
    assert len(R) >= 5 and len(REFITS) == len(R)
    assert PREDICTION_ONLY == 0


def test_warmup_nulls_derived_from_the_config() -> None:
    expected = SMALL_CFG.point_in_time_warmup  # window + zscore_min_obs - 1 + min_train_days
    assert R[0] == expected
    assert FULL["regime"].isna().sum() == expected
    assert FULL["regime"].iloc[:expected].isna().all()
    assert FULL["regime"].iloc[expected:].notna().all()
    for column in (*PROB_COLUMNS, "train_end_date", "train_rows"):
        assert FULL[column].isna().sum() == expected


@pytest.mark.parametrize(
    "cut",
    ["first_fit", "first_fit+1", "second_fit-1", "second_fit", "mid_segment", "last_fit", "end-1"],
)
def test_truncation_reproduces_every_earlier_row(cut: str) -> None:
    """Rows with trade_date <= D are identical when rebuilt from data up to D only."""
    d = {
        "first_fit": R[0],
        "first_fit+1": R[0] + 1,
        "second_fit-1": R[1] - 1,
        "second_fit": R[1],
        "mid_segment": R[2] + SMALL_CFG.refit_every_days // 2,
        "last_fit": R[-1],
        "end-1": len(FEATURES) - 2,
    }[cut]
    truncated, refits, _ = build_point_in_time(FEATURES.iloc[: d + 1], SMALL_CFG)
    pd.testing.assert_frame_equal(truncated, FULL.iloc[: d + 1])
    assert [r.first_day for r in refits] == [r.first_day for r in REFITS[: len(refits)]]
    assert [r.log_likelihood for r in refits] == [r.log_likelihood for r in REFITS[: len(refits)]]


def test_changing_the_future_leaves_the_past_unchanged() -> None:
    d = R[2] + 10
    perturbed = FEATURES.copy()
    perturbed.iloc[d + 1 :] = perturbed.iloc[d + 1 :].to_numpy()[::-1] * 3.0 - 1.0
    rebuilt, _, _ = build_point_in_time(perturbed, SMALL_CFG)
    pd.testing.assert_frame_equal(rebuilt.iloc[: d + 1], FULL.iloc[: d + 1])
    assert not rebuilt.iloc[d + 1 :][list(PROB_COLUMNS)].equals(
        FULL.iloc[d + 1 :][list(PROB_COLUMNS)]
    )


def test_probabilities_are_filtered_not_smoothed() -> None:
    """The last segment equals a forward filter with that segment's fit, and differs from the
    smoothed posteriors of the same fit."""
    z = expanding_zscore(FEATURES, SMALL_CFG.zscore_min_obs).to_numpy()
    valid = ~np.isnan(z).any(axis=1)
    r = R[-1]
    fit = _fit(z[:r][valid[:r]], SMALL_CFG)
    start = int(np.argmax(valid))
    filtered, _ = forward_filter(z[start:], fit.params)
    stored = FULL[list(PROB_COLUMNS)].to_numpy()
    np.testing.assert_array_equal(stored[r:], filtered[r - start :])
    smooth = smoothed(fit, z[start:])
    assert not np.allclose(stored[r:], smooth[r - start :])


def test_labels_are_argmax_and_probabilities_sum_to_one() -> None:
    labelled = FULL.dropna(subset=["regime"])
    probs = labelled[list(PROB_COLUMNS)].to_numpy()
    np.testing.assert_allclose(probs.sum(axis=1), 1.0)
    names = np.array([c.removeprefix("p_") for c in PROB_COLUMNS])
    assert (names[probs.argmax(axis=1)] == labelled["regime"].to_numpy()).all()


def test_states_follow_volatility_after_each_refit() -> None:
    for refit in REFITS:
        assert list(refit.vol_means) == sorted(refit.vol_means)


def test_training_ends_before_each_refit() -> None:
    labelled = FULL.dropna(subset=["regime"])
    assert (labelled["train_end_date"] < labelled["trade_date"]).all()
    for refit in REFITS:
        assert refit.train_end_date < refit.first_day


def test_full_sample_is_flagged_and_uses_the_whole_sample() -> None:
    full_sample, refits = build_full_sample(FEATURES, SMALL_CFG)
    assert not full_sample["is_point_in_time"].any()
    assert (full_sample["variant"] == FULL_SAMPLE).all()
    assert full_sample["regime"].isna().sum() == SMALL_CFG.vol_window
    assert len(refits) == 1 and refits[0].train_rows == len(FEATURES) - SMALL_CFG.vol_window
    # not point-in-time: truncation changes earlier rows
    truncated, _ = build_full_sample(FEATURES.iloc[: R[2]], SMALL_CFG)
    assert not np.allclose(
        truncated[list(PROB_COLUMNS)].iloc[R[0] : R[1]].to_numpy(),
        full_sample[list(PROB_COLUMNS)].iloc[R[0] : R[1]].to_numpy(),
    )


def test_build_is_deterministic_and_stacks_both_variants() -> None:
    a, b = build_regimes_v2(FEATURES, SMALL_CFG), build_regimes_v2(FEATURES, SMALL_CFG)
    pd.testing.assert_frame_equal(a.frame, b.frame)
    assert a.refits == b.refits
    assert a.frame.groupby("variant").size().to_dict() == {
        POINT_IN_TIME: len(FEATURES),
        FULL_SAMPLE: len(FEATURES),
    }
    pit = a.frame[a.frame["variant"] == POINT_IN_TIME].reset_index(drop=True)
    pd.testing.assert_frame_equal(pit, FULL)


def test_missing_feature_after_warmup_is_a_prediction_step() -> None:
    gap = FEATURES.copy()
    d = R[1] + 5
    gap.iloc[d, gap.columns.get_loc("nfci")] = np.nan
    frame, _, prediction_only = build_point_in_time(gap, SMALL_CFG)
    assert prediction_only == 1
    assert frame["regime"].iloc[d] is not None
    pd.testing.assert_frame_equal(frame.iloc[:d], FULL.iloc[:d])


def test_no_fit_when_history_is_too_short() -> None:
    short = FEATURES.iloc[: SMALL_CFG.point_in_time_warmup]
    frame, refits, _ = build_point_in_time(short, SMALL_CFG)
    assert refits == [] and frame["regime"].isna().all()
