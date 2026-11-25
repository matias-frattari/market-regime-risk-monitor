# SYNTHETIC — not real market data: observations simulated from a made-up 3-state Gaussian
# HMM (seeded), only to test fitting, state ordering and the forward filter.
import math

import numpy as np
import pytest

from mrm.regimes.hmm import (
    HmmParams,
    fit_best,
    forward_filter,
    order_states,
    params_of,
    smoothed,
)

TRUE = HmmParams(
    startprob=np.array([0.2, 0.5, 0.3]),
    transmat=np.array([[0.95, 0.04, 0.01], [0.03, 0.95, 0.02], [0.02, 0.08, 0.90]]),
    # state 0 has the highest mean of feature 0 (volatility), state 1 the lowest
    means=np.array([[2.5, 1.0, -1.0], [-0.8, -0.5, 0.5], [0.6, 0.3, 0.0]]),
    variances=np.array([[0.5, 0.3, 0.4], [0.1, 0.2, 0.2], [0.2, 0.2, 0.3]]),
)
FIT = dict(n_states=3, n_init=2, seed=0, max_iter=100, tol=1e-4, order_column=0)


def simulate(n: int = 600, seed: int = 11) -> tuple[np.ndarray, np.ndarray]:
    rng = np.random.default_rng(seed)
    states = np.empty(n, dtype=int)
    states[0] = rng.choice(3, p=TRUE.startprob)
    for t in range(1, n):
        states[t] = rng.choice(3, p=TRUE.transmat[states[t - 1]])
    x = TRUE.means[states] + rng.normal(size=(n, 3)) * np.sqrt(TRUE.variances[states])
    return x, states


def reference_filter(x: np.ndarray, p: HmmParams) -> np.ndarray:
    """Textbook forward recursion with explicit densities (no shared code with the module)."""
    out = []
    alpha = None
    for t, row in enumerate(x):
        dens = np.array(
            [
                math.prod(
                    math.exp(-((v - m) ** 2) / (2 * s)) / math.sqrt(2 * math.pi * s)
                    for v, m, s in zip(row, p.means[k], p.variances[k], strict=True)
                )
                for k in range(3)
            ]
        )
        prior = p.startprob if t == 0 else alpha @ p.transmat
        alpha = prior * dens / (prior * dens).sum()
        out.append(alpha)
    return np.array(out)


def test_forward_filter_matches_reference_recursion() -> None:
    x, _ = simulate(200)
    probs, _ = forward_filter(x, TRUE)
    np.testing.assert_allclose(probs, reference_filter(x, TRUE), rtol=1e-9, atol=1e-12)
    np.testing.assert_allclose(probs.sum(axis=1), 1.0)


def test_forward_log_likelihood_matches_hmmlearn_score() -> None:
    x, _ = simulate()
    fit = fit_best(x, **FIT)
    _, log_likelihood = forward_filter(x, params_of(fit.model))
    assert log_likelihood == pytest.approx(fit.model.score(x), rel=1e-9)
    assert log_likelihood == pytest.approx(fit.log_likelihood, rel=1e-9)


def test_filtered_probabilities_ignore_the_future_smoothed_do_not() -> None:
    x, _ = simulate()
    fit = fit_best(x, **FIT)
    changed = x.copy()
    changed[400:] = changed[400:][::-1] * -1.5
    filtered, _ = forward_filter(x, fit.params)
    filtered_changed, _ = forward_filter(changed, fit.params)
    np.testing.assert_array_equal(filtered[:400], filtered_changed[:400])
    smooth, smooth_changed = smoothed(fit, x), smoothed(fit, changed)
    assert not np.allclose(smooth[:400], smooth_changed[:400])
    assert not np.allclose(filtered, smooth)  # the official version is not the smoothed one


def test_states_ordered_by_volatility_mean() -> None:
    x, states = simulate()
    fit = fit_best(x, **FIT)
    assert np.all(np.diff(fit.params.means[:, 0]) > 0)
    # ordering is a relabelling: filtered probabilities are permuted, not changed
    raw, _ = forward_filter(x, params_of(fit.model))
    ordered, _ = forward_filter(x, fit.params)
    np.testing.assert_allclose(ordered, raw[:, fit.order], atol=1e-12)
    # the fitted "stress" state is the simulated high-volatility state 0 on most days
    assert (ordered.argmax(axis=1)[states == 0] == 2).mean() > 0.9


def test_order_states_permutes_every_parameter() -> None:
    ordered, order = order_states(TRUE, column=0)
    assert order.tolist() == [1, 2, 0]
    assert ordered.transmat[0, 0] == TRUE.transmat[1, 1]
    assert ordered.transmat[0, 2] == TRUE.transmat[1, 0]
    np.testing.assert_array_equal(ordered.variances[2], TRUE.variances[0])
    np.testing.assert_allclose(ordered.transmat.sum(axis=1), 1.0)


def test_missing_row_is_a_prediction_step() -> None:
    x, _ = simulate(50)
    x[20, 1] = np.nan
    probs, _ = forward_filter(x, TRUE)
    np.testing.assert_allclose(probs[20], probs[19] @ TRUE.transmat)
    assert not np.isnan(probs).any()


def test_fit_is_deterministic() -> None:
    x, _ = simulate()
    a, b = fit_best(x, **FIT), fit_best(x, **FIT)
    assert a.log_likelihood == b.log_likelihood and a.seed == b.seed
    for field in ("startprob", "transmat", "means", "variances"):
        np.testing.assert_array_equal(getattr(a.params, field), getattr(b.params, field))


def test_best_of_n_init_is_kept() -> None:
    x, _ = simulate()
    best = fit_best(x, **{**FIT, "n_init": 3})
    singles = [fit_best(x, **{**FIT, "n_init": 1, "seed": s}) for s in range(3)]
    assert best.log_likelihood == max(f.log_likelihood for f in singles)


def test_fit_rejects_missing_rows() -> None:
    x, _ = simulate(50)
    x[3, 0] = np.nan
    with pytest.raises(ValueError, match="NaN"):
        fit_best(x, **FIT)
