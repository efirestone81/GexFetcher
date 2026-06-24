"""Black-Scholes gamma (and call price) for European options on a
dividend-paying underlying."""
import math
from typing import Union
import numpy as np

ArrayLike = Union[float, np.ndarray]
T_MIN = 0.5 / 365.0
IV_MIN = 0.05
IV_MAX = 3.0


def norm_pdf(x: ArrayLike) -> ArrayLike:
    """Standard normal probability density function."""
    return np.exp(-0.5 * np.asarray(x, dtype=np.float64) ** 2) / math.sqrt(2.0 * math.pi)


def norm_cdf(x: ArrayLike) -> ArrayLike:
    """Standard normal cumulative distribution function (via erf)."""
    x = np.asarray(x, dtype=np.float64)
    try:
        from scipy.special import erf  # type: ignore
        return 0.5 * (1.0 + erf(x / math.sqrt(2.0)))
    except Exception:
        vec = np.vectorize(lambda v: 0.5 * (1.0 + math.erf(v / math.sqrt(2.0))))
        return vec(x)


def _d1_d2(S, K, T, r, q, sigma):
    S = np.asarray(S, dtype=np.float64)
    K = np.asarray(K, dtype=np.float64)
    T = np.asarray(T, dtype=np.float64)
    sigma = np.asarray(sigma, dtype=np.float64)
    valid = (S > 0) & (K > 0) & (T > 0) & (sigma > 0)
    sqrtT = np.sqrt(np.where(valid, T, 1.0))
    d1 = (
        np.log(np.where(valid, S / K, 1.0))
        + (r - q + 0.5 * np.where(valid, sigma, 1.0) ** 2) * np.where(valid, T, 1.0)
    ) / (np.where(valid, sigma, 1.0) * sqrtT)
    d2 = d1 - np.where(valid, sigma, 1.0) * sqrtT
    return d1, d2, valid


def bs_gamma(S, K, T, r, q, sigma):
    """Black-Scholes gamma. Returns 0 for invalid inputs."""
    with np.errstate(divide="ignore", invalid="ignore"):
        d1, _, valid = _d1_d2(S, K, T, r, q, sigma)
        S_arr = np.asarray(S, dtype=np.float64)
        T_arr = np.asarray(T, dtype=np.float64)
        sigma_arr = np.asarray(sigma, dtype=np.float64)
        sqrtT = np.sqrt(np.where(valid, T_arr, 1.0))
        gamma = (
            np.exp(-q * np.where(valid, T_arr, 0.0))
            * norm_pdf(d1)
            / (np.where(valid, S_arr, 1.0) * np.where(valid, sigma_arr, 1.0) * sqrtT)
        )
    return np.where(valid, gamma, 0.0)


def bs_call_price(S, K, T, r, q, sigma):
    """Black-Scholes price of a European call on a dividend-paying underlying.

    C = S e^{-qT} N(d1) - K e^{-rT} N(d2)
    Returns intrinsic value max(S-K, 0) when T or sigma is non-positive.
    """
    with np.errstate(divide="ignore", invalid="ignore"):
        d1, d2, valid = _d1_d2(S, K, T, r, q, sigma)
        S_arr = np.asarray(S, dtype=np.float64)
        K_arr = np.asarray(K, dtype=np.float64)
        T_arr = np.asarray(T, dtype=np.float64)
        call = (
            S_arr * np.exp(-q * T_arr) * norm_cdf(d1)
            - K_arr * np.exp(-r * T_arr) * norm_cdf(d2)
        )
        intrinsic = np.maximum(S_arr - K_arr, 0.0)
    return np.where(valid, call, intrinsic)


def bs_call_delta(S, K, T, r, q, sigma):
    """Delta of a European call: e^{-qT} N(d1)."""
    with np.errstate(divide="ignore", invalid="ignore"):
        d1, _, valid = _d1_d2(S, K, T, r, q, sigma)
        T_arr = np.asarray(T, dtype=np.float64)
        delta = np.exp(-q * T_arr) * norm_cdf(d1)
    return np.where(valid, delta, 0.0)


def bs_put_delta(S, K, T, r, q, sigma):
    """Delta of a European put: e^{-qT} (N(d1) - 1)."""
    with np.errstate(divide="ignore", invalid="ignore"):
        d1, _, valid = _d1_d2(S, K, T, r, q, sigma)
        T_arr = np.asarray(T, dtype=np.float64)
        delta = np.exp(-q * T_arr) * (norm_cdf(d1) - 1.0)
    return np.where(valid, delta, 0.0)