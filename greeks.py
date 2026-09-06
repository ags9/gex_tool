"""Black-Scholes greeks engine — vectorized, includes the second-order
greeks (vanna, charm) the exposure ledgers need. European-style, which is
exactly right for SPX/XSP.

Conventions: t in years, r continuously compounded, q dividend yield.
All functions accept scalars or numpy arrays and broadcast.
"""
from __future__ import annotations

import numpy as np
from numpy.typing import ArrayLike

SQRT_2PI = np.sqrt(2.0 * np.pi)


def _norm_pdf(x):
    return np.exp(-0.5 * np.square(x)) / SQRT_2PI


def _norm_cdf(x):
    from math import erf
    x = np.asarray(x, dtype=float)
    return 0.5 * (1.0 + np.vectorize(erf)(x / np.sqrt(2.0)))


def d1_d2(s, k, t, iv, r=0.0, q=0.0):
    s, k, t, iv = (np.asarray(a, dtype=float) for a in (s, k, t, iv))
    t = np.maximum(t, 1e-8)
    iv = np.maximum(iv, 1e-8)
    d1 = (np.log(s / k) + (r - q + 0.5 * iv**2) * t) / (iv * np.sqrt(t))
    return d1, d1 - iv * np.sqrt(t)


def price(s, k, t, iv, right, r=0.0, q=0.0):
    d1, d2 = d1_d2(s, k, t, iv, r, q)
    s, k, t = (np.asarray(a, dtype=float) for a in (s, k, t))
    call = s * np.exp(-q * t) * _norm_cdf(d1) - k * np.exp(-r * t) * _norm_cdf(d2)
    if isinstance(right, str):
        if right == "C":
            return call
        return call - s * np.exp(-q * t) + k * np.exp(-r * t)  # put-call parity
    is_call = np.asarray(right) == "C"
    put = call - s * np.exp(-q * t) + k * np.exp(-r * t)
    return np.where(is_call, call, put)


def delta(s, k, t, iv, right, r=0.0, q=0.0):
    d1, _ = d1_d2(s, k, t, iv, r, q)
    t = np.asarray(t, dtype=float)
    call_delta = np.exp(-q * t) * _norm_cdf(d1)
    if isinstance(right, str):
        return call_delta if right == "C" else call_delta - np.exp(-q * t)
    return np.where(np.asarray(right) == "C", call_delta, call_delta - np.exp(-q * t))


def gamma(s, k, t, iv, r=0.0, q=0.0):
    """Same for calls and puts."""
    d1, _ = d1_d2(s, k, t, iv, r, q)
    s, t, iv = (np.asarray(a, dtype=float) for a in (s, t, iv))
    return np.exp(-q * t) * _norm_pdf(d1) / (s * iv * np.sqrt(np.maximum(t, 1e-8)))


def vanna(s, k, t, iv, r=0.0, q=0.0):
    """dDelta/dVol (per 1.0 of vol). Same for calls and puts."""
    d1, d2 = d1_d2(s, k, t, iv, r, q)
    t, iv = np.asarray(t, dtype=float), np.asarray(iv, dtype=float)
    return -np.exp(-q * t) * _norm_pdf(d1) * d2 / iv


def charm(s, k, t, iv, right, r=0.0, q=0.0):
    """dDelta/dTime (per year; divide by 252 for per-trading-day)."""
    d1, d2 = d1_d2(s, k, t, iv, r, q)
    t, iv = np.maximum(np.asarray(t, float), 1e-8), np.asarray(iv, float)
    common = np.exp(-q * t) * _norm_pdf(d1) * (
        2.0 * (r - q) * t - d2 * iv * np.sqrt(t)
    ) / (2.0 * t * iv * np.sqrt(t))
    if isinstance(right, str):
        adj = q * np.exp(-q * t) * _norm_cdf(d1) if right == "C" \
            else -q * np.exp(-q * t) * _norm_cdf(-d1)
        return -common + adj
    is_call = np.asarray(right) == "C"
    adj = np.where(is_call,
                   q * np.exp(-q * t) * _norm_cdf(d1),
                   -q * np.exp(-q * t) * _norm_cdf(-d1))
    return -common + adj


def implied_vol(mkt_price, s, k, t, right, r=0.0, q=0.0,
                lo=0.005, hi=5.0, tol=1e-6, max_iter=80):
    """Robust bisection IV solver (vectorized). Returns nan where the price
    is outside no-arbitrage bounds."""
    mkt = np.asarray(mkt_price, dtype=float)
    lo_a = np.full_like(mkt, lo, dtype=float)
    hi_a = np.full_like(mkt, hi, dtype=float)
    p_lo = price(s, k, t, lo_a, right, r, q)
    p_hi = price(s, k, t, hi_a, right, r, q)
    bad = (mkt < p_lo) | (mkt > p_hi)
    for _ in range(max_iter):
        mid = 0.5 * (lo_a + hi_a)
        p_mid = price(s, k, t, mid, right, r, q)
        below = p_mid < mkt
        lo_a = np.where(below, mid, lo_a)
        hi_a = np.where(below, hi_a, mid)
        if np.all(hi_a - lo_a < tol):
            break
    out = 0.5 * (lo_a + hi_a)
    return np.where(bad, np.nan, out)
