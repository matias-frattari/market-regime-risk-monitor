"""Gaussian HMM for regimes v2 (ADR 0007): fit, state ordering, filtered probabilities.

hmmlearn is used **only to fit** (EM). Filtered probabilities ``P(s_t | x_1..x_t)`` come from
:func:`forward_filter`, a forward recursion in numpy on the fitted parameters, so the official
point-in-time version never touches hmmlearn's Viterbi or smoothed posteriors. Smoothed
posteriors (:func:`smoothed`) exist only for the descriptive full-sample version.
"""

import logging
from dataclasses import dataclass

import numpy as np
from hmmlearn.hmm import GaussianHMM
from threadpoolctl import threadpool_limits

LOG_2PI = float(np.log(2.0 * np.pi))


@dataclass(frozen=True, slots=True)
class HmmParams:
    """Parameters of a Gaussian HMM with diagonal covariance (``k`` states, ``f`` features)."""

    startprob: np.ndarray  # (k,)
    transmat: np.ndarray  # (k, k), rows sum to 1
    means: np.ndarray  # (k, f)
    variances: np.ndarray  # (k, f)


@dataclass(frozen=True, slots=True)
class FitResult:
    """Best of ``n_init`` EM runs, states already ordered (see :func:`order_states`).

    ``order[j]`` is the hmmlearn state that became ordered state ``j``; ``model`` keeps
    hmmlearn's own (unordered) state numbering.
    """

    params: HmmParams
    order: np.ndarray
    log_likelihood: float
    n_iter: int
    converged: bool  # EM stopped on ``tol`` before ``max_iter``
    seed: int
    model: GaussianHMM


def params_of(model: GaussianHMM) -> HmmParams:
    """Copy the fitted parameters of a diagonal ``GaussianHMM`` (hmmlearn state order)."""
    return HmmParams(
        startprob=np.array(model.startprob_, dtype=float),
        transmat=np.array(model.transmat_, dtype=float),
        means=np.array(model.means_, dtype=float),
        variances=np.array([np.diag(c) for c in model.covars_], dtype=float),
    )


def order_states(params: HmmParams, column: int) -> tuple[HmmParams, np.ndarray]:
    """Reorder states by ascending emission mean of feature ``column`` (stable for ties)."""
    order = np.argsort(params.means[:, column], kind="stable")
    ordered = HmmParams(
        startprob=params.startprob[order],
        transmat=params.transmat[np.ix_(order, order)],
        means=params.means[order],
        variances=params.variances[order],
    )
    return ordered, order


def fit_best(
    x: np.ndarray,
    *,
    n_states: int,
    n_init: int,
    seed: int,
    max_iter: int,
    tol: float,
    order_column: int,
) -> FitResult:
    """Fit ``n_init`` diagonal Gaussian HMMs (seeds ``seed + i``) on the rows of ``x`` (no NaN)
    and keep the one with the highest log-likelihood (the earliest seed on ties).

    Runs single-threaded (OpenMP/BLAS): multithreaded k-means initialisation sums in a
    varying order, which changes the last bits of the fit from one run to the next.
    """
    if np.isnan(x).any():
        raise ValueError("fit_best needs complete rows (no NaN)")
    best: FitResult | None = None
    logger = logging.getLogger("hmmlearn")
    level = logger.level
    logger.setLevel(logging.ERROR)  # EM progress warnings; convergence is reported instead
    try:
        with threadpool_limits(limits=1):
            for i in range(n_init):
                model = GaussianHMM(
                    n_components=n_states,
                    covariance_type="diag",
                    n_iter=max_iter,
                    tol=tol,
                    random_state=seed + i,
                )
                model.fit(x)
                log_likelihood = float(model.score(x))
                if best is None or log_likelihood > best.log_likelihood:
                    params, order = order_states(params_of(model), order_column)
                    n_iter = int(model.monitor_.iter)
                    best = FitResult(
                        params=params,
                        order=order,
                        log_likelihood=log_likelihood,
                        n_iter=n_iter,
                        converged=n_iter < max_iter,
                        seed=seed + i,
                        model=model,
                    )
    finally:
        logger.setLevel(level)
    assert best is not None  # n_init >= 1
    return best


def log_emissions(x: np.ndarray, params: HmmParams) -> np.ndarray:
    """``log N(x_t; mean_k, diag(var_k))`` per row and state, shape (n, k). NaN rows -> NaN."""
    diff = x[:, None, :] - params.means[None, :, :]
    quad = (diff**2 / params.variances[None, :, :]).sum(axis=2)
    log_det = np.log(params.variances).sum(axis=1)
    return -0.5 * (x.shape[1] * LOG_2PI + log_det[None, :] + quad)


def forward_filter(x: np.ndarray, params: HmmParams) -> tuple[np.ndarray, float]:
    """Filtered state probabilities ``P(s_t | x_1..x_t)`` (shape (n, k)) and the
    log-likelihood of the rows with an emission.

    Forward recursion with per-step normalisation: row ``t`` uses rows ``<= t`` only. A row
    with any NaN feature is a prediction-only step: ``P(s_t | x_1..x_{t-1})``, no emission.
    """
    log_b = log_emissions(x, params)
    missing = np.isnan(log_b).any(axis=1)
    probs = np.empty_like(log_b)
    log_likelihood = 0.0
    prior = params.startprob
    for t in range(len(x)):
        if t > 0:
            prior = probs[t - 1] @ params.transmat
        if missing[t]:
            probs[t] = prior
            continue
        shift = log_b[t].max()
        alpha = prior * np.exp(log_b[t] - shift)
        total = alpha.sum()
        probs[t] = alpha / total
        log_likelihood += float(np.log(total) + shift)
    return probs, log_likelihood


def smoothed(fit: FitResult, x: np.ndarray) -> np.ndarray:
    """Smoothed posteriors ``P(s_t | x_1..x_n)`` in ordered states (forward-backward).

    **Uses future observations: never for the point-in-time version.**
    """
    return fit.model.predict_proba(x)[:, fit.order]
