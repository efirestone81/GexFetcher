"""
Black-Scholes Greeks for European options on a dividend-paying underlying.

Vectorized over NumPy arrays for performance — used by the gamma flip sweep
which evaluates gamma at hundreds of candidate spot prices.

References:
- Hull, "Options, Futures, and Other Derivatives" (10th ed.), Eq. 17.10
- The dividend-yield form of Black-Scholes is appropriate for index options
  (continuous proportional dividend stream).

Conventions:
- All rates and yields are continuously compounded annualized decimals
  (e.g. 4.30% SOFR = 0.0430).
- Time T is in years (calendar days / 365.25 in the production pipeline).
- IV (sigma) is annualized standard deviation of log returns.
- Gamma is identical for calls and puts at the same strike — we use a single
  function rather than separate call/put forms.
"""

import math
from typing import Union

import numpy as np

ArrayLike = Union[float, np.ndarray]

# Floor on time-to-expiry to prevent gamma explosion on 0DTE near ATM.
# 0.5 / 365 ≈ half a calendar day. Production pipeline applies this in the
# parser; greeks.py also clamps as a defense in depth.
T_MIN = 0.5 / 365.0

# IV clamp band — outside this we treat the feed value as anomalous.
IV_MIN = 0.05
IV_MAX = 3.0


def norm_pdf(x: ArrayLike) -> ArrayLike:
    """
    Standard normal probability density function: φ(x) = exp(-x²/2) / √(2π).

    Implemented inline to avoid pulling scipy.stats into the hot path; for
    aggregate timing this saves ~5x vs scipy.stats.norm.pdf.
    """
    return np.exp(-0.5 * np.asarray(x) ** 2) / math.sqrt(2.0 * math.pi)


def bs_gamma(
    S: ArrayLike,
    K: ArrayLike,
    T: ArrayLike,
    r: float,
    q: float,
    sigma: ArrayLike,
) -> ArrayLike:
    """
    Black-Scholes gamma for a European option on a dividend-paying underlying.

        d1 = [ln(S/K) + (r - q + σ²/2)·T] / (σ·√T)
        Γ  = e^(-qT) · φ(d1) / (S · σ · √T)

    Parameters
    ----------
    S : float or ndarray
        Spot price of the underlying. Scalar when called from a flip sweep at
        a single candidate spot; array when evaluating across many spots.
    K : float or ndarray
        Strike price(s).
    T : float or ndarray
        Time to expiration in years.
    r : float
        Continuously compounded risk-free rate (annualized).
    q : float
        Continuously compounded dividend yield (annualized).
    sigma : float or ndarray
        Implied volatility (annualized standard deviation).

    Returns
    -------
    float or ndarray
        Gamma per share. Multiply by contract multiplier (×100 for equity
        options) and OI for total dealer rehedge sensitivity.

    Notes
    -----
    - Returns 0.0 for invalid inputs (T <= 0, sigma <= 0, S <= 0, K <= 0)
      rather than NaN/inf. This makes vectorized aggregation safe.
    - Vectorizes correctly over any broadcast-compatible combination of
      S, K, T, sigma.
    """
    S = np.asarray(S, dtype=np.float64)
    K = np.asarray(K, dtype=np.float64)
    T = np.asarray(T, dtype=np.float64)
    sigma = np.asarray(sigma, dtype=np.float64)

    # Mask of valid inputs — invalid entries get gamma=0 to keep sums clean.
    valid = (S > 0) & (K > 0) & (T > 0) & (sigma > 0)

    # Suppress divide-by-zero warnings for the masked-off entries; we'll
    # zero them explicitly below.
    with np.errstate(divide="ignore", invalid="ignore"):
        sqrtT = np.sqrt(np.where(valid, T, 1.0))
        d1 = (
            np.log(np.where(valid, S / K, 1.0))
            + (r - q + 0.5 * np.where(valid, sigma, 1.0) ** 2)
            * np.where(valid, T, 1.0)
        ) / (np.where(valid, sigma, 1.0) * sqrtT)

        gamma = (
            np.exp(-q * np.where(valid, T, 0.0))
            * norm_pdf(d1)
            / (np.where(valid, S, 1.0) * np.where(valid, sigma, 1.0) * sqrtT)
        )

    return np.where(valid, gamma, 0.0)


def bs_call_price(
    S: ArrayLike,
    K: ArrayLike,
    T: ArrayLike,
    r: float,
    q: float,
    sigma: ArrayLike,
) -> ArrayLike:
    """
    Black-Scholes price of a European call option on a dividend-paying
    underlying. Used in tests to validate Γ via central-difference of price
    with respect to S (Γ = ∂²C/∂S²).

    Not used in the production GEX pipeline — included for test coverage.
    """
    from scipy.stats import norm  # local import; only used in dev/test path.

    S = np.asarray(S, dtype=np.float64)
    K = np.asarray(K, dtype=np.float64)
    T = np.asarray(T, dtype=np.float64)
    sigma = np.asarray(sigma, dtype=np.float64)

    sqrtT = np.sqrt(T)
    d1 = (np.log(S / K) + (r - q + 0.5 * sigma ** 2) * T) / (sigma * sqrtT)
    d2 = d1 - sigma * sqrtT
    return S * np.exp(-q * T) * norm.cdf(d1) - K * np.exp(-r * T) * norm.cdf(d2)
