"""Position size from the equity risk budget. Spot long only."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class SizeResult:
    ok: bool
    reason: str
    qty_base: float
    qty_quote: float
    risk_quote: float


def size_order(
    *,
    equity: float,
    cash: float,
    entry: float,
    stop: float,
    fee_rate: float,
    max_risk_pct: float,
) -> SizeResult:
    """Size a market buy so a stop at ``stop`` loses at most ``max_risk_pct``.

    Notional is capped by cash after fees. Capping shrinks risk; it never
    raises it. ``entry`` is the expected fill (the mark), not a limit price.
    """
    if equity <= 0 or entry <= 0 or stop <= 0 or stop >= entry:
        return SizeResult(False, "invalid_stop", 0.0, 0.0, 0.0)
    if fee_rate < 0 or cash < 0 or max_risk_pct <= 0:
        return SizeResult(False, "bad_inputs", 0.0, 0.0, 0.0)
    risk_frac = (entry - stop) / entry
    if risk_frac <= 0:
        return SizeResult(False, "invalid_stop", 0.0, 0.0, 0.0)
    risk_budget = equity * max_risk_pct
    notional = risk_budget / risk_frac
    max_notional = cash / (1.0 + fee_rate) - 1e-6
    if notional > max_notional:
        notional = max_notional
    if notional <= 0:
        return SizeResult(False, "no_cash", 0.0, 0.0, 0.0)
    actual_risk = notional * risk_frac
    if actual_risk > risk_budget + 1e-6:
        return SizeResult(False, "risk_budget", 0.0, notional, actual_risk)
    return SizeResult(True, "ok", notional / entry, notional, actual_risk)
