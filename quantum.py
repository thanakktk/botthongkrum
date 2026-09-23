"""
Quantum finance model: Quantum Price Levels (QPL) from a quantum anharmonic
oscillator (Raymond S. T. Lee, *Quantum Finance*, Springer 2019, ch. 4-5).
======================================================================
Idea: the (standardised) daily price return z = r / sigma is treated as a
"quantum financial particle" moving in the potential

        V(z) = z^2 / 2 + lambda * z^4          (dimensionless units)

Solving the Schroedinger equation  -1/2 psi'' + V psi = E psi  gives discrete
energy levels E_0 < E_1 < E_2 ... . Lee maps them to price levels:

        QPR(n) = E(n) / E(0)                        (quantum price return)
        QPL(+n) = P0 * (1 + 0.21 * sigma * QPR(n))  (resistance ladder)
        QPL(-n) = P0 * (1 - 0.21 * sigma * QPR(n))  (support ladder)

with P0 = the reference close (previous day) and sigma = the standard deviation
of the daily return (in return units, e.g. 0.011 = 1.1 %). 0.21 is Lee's
empirical scaling constant. For lambda = 0 (harmonic) E(n)/E(0) = 2n+1, so the
ladder is 0.21s, 0.63s, 1.05s, 1.47s ... away from P0; lambda > 0 pushes the
upper levels further apart (anharmonic stretching).

lambda is fitted from the data: the ground state |psi_0|^2 is the model's
return density, and its kurtosis falls monotonically from 3 (lambda = 0) as
lambda grows, so we invert the observed kurtosis of z. NOTE: fat-tailed
returns (kurtosis > 3) cannot be produced by this family, so they clip to
lambda = 0 (harmonic ladder). The backtest reports how often that happens.

Everything here is numpy only (a 60x60 matrix diagonalisation per lambda,
cached), so the live bot can call it every H4 bar.
"""

from __future__ import annotations

from functools import lru_cache
import math

import numpy as np

LEE_SCALE = 0.21          # Lee's empirical constant in QPL = P0 (1 +- 0.21 sigma QPR)
BASIS = 60                # harmonic-oscillator basis size for the diagonalisation
N_LEVELS = 12             # energy levels kept (n = 0 .. 11)


def _x_matrix(m: int) -> np.ndarray:
    """Position operator x = (a + a^dagger)/sqrt(2) in the HO basis |0..m-1>."""
    x = np.zeros((m, m))
    for i in range(m - 1):
        x[i, i + 1] = x[i + 1, i] = math.sqrt((i + 1) / 2.0)
    return x


_X = _x_matrix(BASIS)
_X2 = _X @ _X
_X4 = _X2 @ _X2
_H0 = np.diag(np.arange(BASIS) + 0.5)


@lru_cache(maxsize=4096)
def energy_levels(lam: float) -> tuple:
    """E_0..E_{N_LEVELS-1} of H = p^2/2 + x^2/2 + lam x^4 (lam rounded to 1e-3)."""
    lam = round(max(0.0, float(lam)), 3)
    if lam == 0.0:
        return tuple(n + 0.5 for n in range(N_LEVELS))
    w = np.linalg.eigvalsh(_H0 + lam * _X4)
    return tuple(float(v) for v in w[:N_LEVELS])


@lru_cache(maxsize=4096)
def ground_state_kurtosis(lam: float) -> float:
    """Kurtosis <x^4>/<x^2>^2 of |psi_0|^2 for the anharmonic oscillator."""
    lam = round(max(0.0, float(lam)), 3)
    if lam == 0.0:
        return 3.0
    w, v = np.linalg.eigh(_H0 + lam * _X4)
    psi = v[:, 0]
    m2 = float(psi @ _X2 @ psi)
    m4 = float(psi @ _X4 @ psi)
    return m4 / (m2 * m2)


# kurtosis(lambda) is monotone decreasing; tabulate once for the inversion
_LAM_GRID = np.round(np.concatenate([np.arange(0.0, 0.2, 0.005),
                                     np.arange(0.2, 2.0, 0.02),
                                     np.arange(2.0, 10.01, 0.25)]), 3)
_KURT_GRID = np.array([ground_state_kurtosis(l) for l in _LAM_GRID])


def lambda_from_kurtosis(kurt: float) -> float:
    """Invert the ground-state kurtosis; kurt >= 3 (fat tails) -> 0."""
    if not np.isfinite(kurt) or kurt >= 3.0:
        return 0.0
    if kurt <= _KURT_GRID[-1]:
        return float(_LAM_GRID[-1])
    # _KURT_GRID decreases with lambda -> interpolate on the reversed arrays
    return float(np.interp(kurt, _KURT_GRID[::-1], _LAM_GRID[::-1]))


def fit_lambda(returns: np.ndarray) -> float:
    """lambda from a sample of returns (any units; standardised internally)."""
    r = np.asarray(returns, dtype=float)
    r = r[np.isfinite(r)]
    if len(r) < 20:
        return 0.0
    z = r - r.mean()
    m2 = float(np.mean(z * z))
    if m2 <= 0:
        return 0.0
    kurt = float(np.mean(z ** 4)) / (m2 * m2)
    return lambda_from_kurtosis(kurt)


def quantum_price_returns(lam: float, n_levels: int = N_LEVELS) -> np.ndarray:
    """QPR(n) = E(n)/E(0) for n = 0..n_levels-1  (QPR(0) = 1)."""
    e = np.array(energy_levels(lam)[:n_levels])
    return e / e[0]


def qpl_ladder(p0: float, sigma: float, lam: float = 0.0,
               n_levels: int = N_LEVELS, scale: float = LEE_SCALE) -> np.ndarray:
    """Price ladder indexed -n..+n: index n_levels is QPL(0) = p0.

    ladder[n_levels + k] = QPL(+k), ladder[n_levels - k] = QPL(-k), k = 1..n_levels.
    (QPR(0) = 1 is the first rung, so QPL(+1) uses E(0), QPL(+2) uses E(1) ...)
    """
    qpr = quantum_price_returns(lam, n_levels)            # rungs 1..n_levels
    up = p0 * (1.0 + scale * sigma * qpr)
    dn = p0 * (1.0 - scale * sigma * qpr)
    return np.concatenate([dn[::-1], [p0], up])


def level_index(n_levels: int, k: int) -> int:
    """Array index of QPL(k) in a ladder built with `n_levels` (k may be negative)."""
    return n_levels + k


if __name__ == "__main__":       # pure-math self-test, touches no DB
    e0 = energy_levels(0.0)
    assert abs(e0[3] - 3.5) < 1e-12
    e1 = energy_levels(0.1)
    # Bender-Wu / textbook values for lambda = 0.1: E0 = 0.55915, E1 = 1.76950
    assert abs(e1[0] - 0.559146) < 1e-4, e1[0]
    assert abs(e1[1] - 1.769503) < 1e-4, e1[1]
    assert ground_state_kurtosis(0.0) == 3.0
    assert ground_state_kurtosis(0.5) < ground_state_kurtosis(0.1) < 3.0
    assert lambda_from_kurtosis(3.5) == 0.0
    lam = lambda_from_kurtosis(ground_state_kurtosis(0.3))
    assert abs(lam - 0.3) < 0.02, lam
    lad = qpl_ladder(4000.0, 0.01, 0.0, 4)
    assert abs(lad[4] - 4000.0) < 1e-9
    assert abs(lad[5] - 4000 * (1 + 0.21 * 0.01)) < 1e-9
    assert abs(lad[8] - 4000 * (1 + 0.21 * 0.01 * 7)) < 1e-9
    print("E_n(lam=0.1) =", [round(v, 4) for v in e1[:5]])
    print("QPR(lam=0.1) =", np.round(quantum_price_returns(0.1, 6), 3))
    print("ladder(P0=4000, sigma=1%, lam=0):", np.round(lad, 2))
    print("quantum.py self-test OK")
