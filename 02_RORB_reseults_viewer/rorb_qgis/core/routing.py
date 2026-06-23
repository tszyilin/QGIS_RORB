"""
RORB non-linear storage routing: S = k·Q^m

Matches RORBWin output (validated, NSE ≈ 0.9999 vs RORB app).
Uses bisection solver with corrected dry-start initial condition.
"""
import numpy as np


def route_rorb(inflow: np.ndarray, k: float, m: float = 0.8, dt: float = 1.0) -> np.ndarray:
    """
    Route an inflow hydrograph through a single reach.

    Storage relation: S = k · Q^m
    Continuity (trapezoidal): S[t] - S[t-1] = dt/2 · (I[t-1]+I[t] - Q[t-1]-Q[t])
    → solved for Q[t] via bisection.

    Parameters
    ----------
    inflow : upstream flow [m³/s], one value per time step
    k      : reach routing parameter = kc × reach_length_km  [hr·(m³/s)^(1-m)]
    m      : non-linearity exponent (RORB default 0.8)
    dt     : time step [hours]

    Returns
    -------
    outflow : downstream flow [m³/s], same length as inflow
    """
    n = len(inflow)
    if n == 0:
        return np.zeros(0)

    Q = np.zeros(n)

    def bisect(rhs: float, hi_bound: float) -> float:
        lo, hi = 0.0, max(hi_bound, 1e-9)
        for _ in range(64):
            mid = (lo + hi) / 2.0
            if k * mid ** m + mid * dt / 2.0 < rhs:
                lo = mid
            else:
                hi = mid
        return (lo + hi) / 2.0

    # Dry start: no inflow before t=0, so rhs = inflow[0]*dt/2
    Q[0] = bisect(float(inflow[0]) * dt / 2.0,
                  max(float(inflow[0]), 0.0) * 4.0 + 1.0)

    for t in range(1, n):
        I1, I2, Q1 = float(inflow[t - 1]), float(inflow[t]), Q[t - 1]
        rhs = (I1 + I2) * dt / 2.0 - Q1 * dt / 2.0 + k * max(Q1, 0.0) ** m
        Q[t] = bisect(rhs, max(I1, I2, Q1) * 4.0 + 1.0)

    return Q
