"""Hard risk checks. These run in code after the agents have spoken."""

from __future__ import annotations

from dataclasses import dataclass

from dexbot.config import RiskConfig


@dataclass(frozen=True)
class GuardResult:
    allowed: bool
    reason: str


class RiskGuard:
    def __init__(self, cfg: RiskConfig) -> None:
        self.cfg = cfg

    def check(
        self,
        *,
        equity: float,
        day_start_equity: float,
        open_positions: int,
        kill_switch: bool,
        symbol_already_open: bool,
        proposed_risk: float,
    ) -> GuardResult:
        if kill_switch:
            return GuardResult(False, "kill_switch")
        if day_start_equity > 0:
            loss_pct = (day_start_equity - equity) / day_start_equity
            if loss_pct >= self.cfg.max_daily_loss_pct:
                return GuardResult(False, "daily_loss_limit")
        if symbol_already_open:
            return GuardResult(False, "already_open")
        if open_positions >= self.cfg.max_open_positions:
            return GuardResult(False, "max_open_positions")
        limit = equity * self.cfg.max_equity_risk_pct
        if proposed_risk > limit + 1e-6:
            return GuardResult(False, "risk_per_trade")
        return GuardResult(True, "ok")


def reward_risk_ratio(entry: float, stop: float, take_profit: float) -> float | None:
    risk = entry - stop
    reward = take_profit - entry
    if risk <= 0 or reward <= 0 or entry <= 0:
        return None
    return reward / risk


def ratio_ok(ratio: float | None, minimum: float) -> bool:
    return ratio is not None and ratio + 1e-12 >= minimum


@dataclass(frozen=True)
class MarketPlan:
    ok: bool
    reason: str
    ratio: float | None


def assess_market_plan(
    *,
    mark: float,
    entry: float | None,
    stop: float | None,
    take_profit: float | None,
    min_reward_risk: float,
    max_deviation: float,
) -> MarketPlan:
    """Check the prices we would actually fill, which is the current mark."""
    if (
        mark <= 0
        or entry is None
        or stop is None
        or take_profit is None
        or entry <= 0
        or stop <= 0
        or take_profit <= 0
    ):
        return MarketPlan(False, "bad_price", None)
    if abs(mark - entry) / mark > max_deviation + 1e-12:
        return MarketPlan(False, "entry_deviation", None)
    if not (stop < mark < take_profit):
        return MarketPlan(False, "price_order", None)
    ratio = reward_risk_ratio(mark, stop, take_profit)
    if not ratio_ok(ratio, min_reward_risk):
        return MarketPlan(False, "reward_risk", ratio)
    return MarketPlan(True, "ok", ratio)


@dataclass(frozen=True)
class Gate:
    allowed: bool
    reason: str


def execution_gate(
    *,
    team_action: str,
    skeptic_vote: str,
    market_plan: MarketPlan,
    sizing_ok: bool,
    sizing_reason: str,
    guard: GuardResult,
) -> Gate:
    """Buy only when the team lead, the skeptic, and the code limits all agree.

    A skeptic vote of ``wait`` does not block. A vote of ``skip`` does.
    """
    if team_action != "buy":
        return Gate(False, f"team_lead:{team_action}")
    if skeptic_vote == "skip":
        return Gate(False, "skeptic:skip")
    if not market_plan.ok:
        return Gate(False, market_plan.reason)
    if not sizing_ok:
        return Gate(False, f"sizing:{sizing_reason}")
    if not guard.allowed:
        return Gate(False, f"risk_guard:{guard.reason}")
    return Gate(True, "ok")
