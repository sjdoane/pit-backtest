"""PSR, DSR, MinTRL.

Per ADR 0001 decision 4 and ADR 0002 decision 1: implementations must
match the Bailey-LdP 2014 numerical example to within 1e-3. See
docs/research/sources/methodology-backtest-overfitting.md for the formulas
and the worked example.
"""

from __future__ import annotations


def psr(
    sr_hat: float, sr_star: float, T: int, gamma_3: float, gamma_4: float
) -> float:
    """Probabilistic Sharpe Ratio (Bailey-LdP 2012).

    Returns Phi((SR_hat - SR*) * sqrt(T-1) / sqrt(1 - gamma_3 * SR_hat + (gamma_4 - 1)/4 * SR_hat^2)).
    """
    raise NotImplementedError("M4 deliverable")


def dsr(
    sr_hat: float,
    T: int,
    gamma_3: float,
    gamma_4: float,
    v_sr: float,
    n_effective: int,
) -> float:
    """Deflated Sharpe Ratio (Bailey-LdP 2014).

    Verified against the paper's numerical example: SR_hat=1.5, T=60,
    gamma_3=-0.5, gamma_4=5, N=30, V[{SR_n}]=0.4 -> DSR=0.971 (within 1e-3).
    """
    raise NotImplementedError("M4 deliverable")


def min_trl(
    sr_hat: float, sr_star: float, alpha: float, gamma_3: float, gamma_4: float
) -> int:
    """Minimum Track Record Length (Bailey-LdP 2012)."""
    raise NotImplementedError("M4 deliverable")
