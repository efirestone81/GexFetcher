"""Black-Scholes gamma for European options on a dividend-paying underlying."""
import math
from typing import Union
import numpy as np

ArrayLike = Union[float, np.ndarray]
T_MIN = 0.5 / 365.0
IV_MIN = 0.05
IV_MAX = 3.0


def norm_pdf(x: ArrayLike) -> ArrayLike:
    return np.exp(-0.5 * np.asarray(x) ** 2) / math.sqrt(2.0 * math.pi)


def bs_gamma(S, K, T, r, q, sigma):
    S = np.asarray(S, dtype=np.float64)
    K = np.asarray(K, dtype=np.float64)
    T = np.asarray(T, dtype=np.float64)
    sigma = np.asarray(sigma, dtype=np.float64)
    valid = (S > 0) & (K > 0) & (T > 0) & (sigma > 0)
    with np.errstate(divide="ignore", invalid="ignore"):
        sqrtT = np.sqrt(np.where(valid, T, 1.0))
        d1 = (
            np.log(np.where(valid, S / K, 1.0))
            + (r - q + 0.5 * np.where(valid, sigma, 1.0) ** 2) * np.where(valid, T, 1.0)
        ) / (np.where(valid, sigma, 1.0) * sqrtT)
        gamma = (
            np.exp(-q * np.where(valid, T, 0.0))
            * norm_pdf(d1)
            / (np.where(valid, S, 1.0) * np.where(valid, sigma, 1.0) * sqrtT)
        )
    return np.where(valid, gamma, 0.0)
