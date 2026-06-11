"""Rainfall input and loss model processing."""

import numpy as np
import re


# ── Loss models ───────────────────────────────────────────────────────────────

def apply_il_cl(rainfall_mm: np.ndarray, il_mm: float, cl_mm_hr: float,
                dt_hr: float) -> np.ndarray:
    """
    Initial Loss / Continuing Loss (IL/CL) — most common Australian loss model.

    - IL is subtracted first (once-off at start of storm)
    - CL is subtracted at every time step after IL is exhausted
    - Losses only apply to the pervious fraction (handled in simulation.py via fi)

    Returns excess rainfall [mm per time step].
    """
    excess = np.zeros(len(rainfall_mm))
    remaining_il = float(il_mm)
    cl_per_step = float(cl_mm_hr) * float(dt_hr)

    for t, r in enumerate(rainfall_mm):
        r = float(r)
        if remaining_il > 0.0:
            applied = min(r, remaining_il)
            r -= applied
            remaining_il -= applied
        r = max(0.0, r - cl_per_step)
        excess[t] = r

    return excess


def apply_proportional_loss(rainfall_mm: np.ndarray, loss_fraction: float) -> np.ndarray:
    """Proportional (percentage) loss: excess = (1 - loss_fraction) * rainfall."""
    return np.maximum(0.0, rainfall_mm * (1.0 - float(loss_fraction)))


# ── Temporal patterns ─────────────────────────────────────────────────────────

def uniform_pattern(total_mm: float, n_steps: int) -> np.ndarray:
    """Constant intensity — equal depth in every time step."""
    return np.full(n_steps, total_mm / max(n_steps, 1))


def triangular_pattern(total_mm: float, n_steps: int,
                       peak_position: float = 0.33) -> np.ndarray:
    """
    Triangular temporal pattern.
    peak_position: fraction of duration where peak intensity occurs (default 1/3).
    """
    t = np.linspace(0.0, 1.0, n_steps, endpoint=False) + 0.5 / n_steps
    pk = max(1e-6, min(1.0 - 1e-6, peak_position))
    pattern = np.where(t <= pk, t / pk, (1.0 - t) / (1.0 - pk))
    pattern = np.maximum(pattern, 0.0)
    s = pattern.sum()
    return (pattern / s * total_mm) if s > 0 else uniform_pattern(total_mm, n_steps)


def parse_pattern(text: str, total_mm: float = None, n_steps: int = None) -> np.ndarray:
    """
    Parse a user-entered comma/space/newline separated list of depths [mm].
    If total_mm is given, normalise the pattern to that total.
    If n_steps is given and differs from parsed length, resample by interpolation.
    """
    vals = [float(v) for v in re.split(r'[,\s\n]+', text.strip()) if v]
    if not vals:
        raise ValueError("No values found in pattern text.")
    arr = np.array(vals, dtype=float)
    if total_mm is not None and arr.sum() > 0:
        arr = arr / arr.sum() * total_mm
    if n_steps is not None and n_steps != len(arr):
        x_old = np.linspace(0, 1, len(arr))
        x_new = np.linspace(0, 1, n_steps)
        arr = np.interp(x_new, x_old, arr)
        if total_mm is not None and arr.sum() > 0:
            arr = arr / arr.sum() * total_mm
    return arr
