import pytest

from dexbot.config import RiskConfig
from dexbot.risk import GuardResult, MarketPlan, RiskGuard, assess_market_plan, execution_gate


def _guard(**kwargs):
    base = dict(
        equity=10_000,
        day_start_equity=10_000,
        open_positions=0,
        kill_switch=False,
        symbol_already_open=False,
        proposed_risk=100,
    )
    base.update(kwargs)
    return RiskGuard(RiskConfig()).check(**base)


def test_risk_per_trade_cap():
    assert _guard(proposed_risk=100).allowed
    blocked = _guard(proposed_risk=150)
    assert not blocked.allowed
    assert blocked.reason == "risk_per_trade"


def test_max_positions_daily_loss_and_kill_switch():
    assert _guard(open_positions=2).allowed
    blocked = _guard(open_positions=3)
    assert blocked.reason == "max_open_positions"
    assert _guard(symbol_already_open=True).reason == "already_open"
    assert not _guard(equity=9_700).allowed
    assert _guard(equity=9_700).reason == "daily_loss_limit"
    # 1% of the reduced equity is under 100, so the probe uses a risk that still fits.
    assert _guard(equity=9_700.01, proposed_risk=90).allowed
    assert _guard(kill_switch=True).reason == "kill_switch"


def test_reward_risk_boundary():
    ok = assess_market_plan(
        mark=100, entry=100, stop=95, take_profit=110, min_reward_risk=2, max_deviation=0.02
    )
    assert ok.ok
    assert ok.ratio == pytest.approx(2)
    poor = assess_market_plan(
        mark=100, entry=100, stop=98, take_profit=101, min_reward_risk=2, max_deviation=0.02
    )
    assert not poor.ok
    assert poor.reason == "reward_risk"
    far = assess_market_plan(
        mark=90, entry=100, stop=80, take_profit=120, min_reward_risk=2, max_deviation=0.02
    )
    assert far.reason == "entry_deviation"


def test_execution_gate_requires_buy_and_not_skip():
    plan = MarketPlan(True, "ok", 2.0)
    guard = GuardResult(True, "ok")
    assert execution_gate(
        team_action="buy",
        skeptic_vote="wait",
        market_plan=plan,
        sizing_ok=True,
        sizing_reason="ok",
        guard=guard,
    ).allowed
    skipped = execution_gate(
        team_action="buy",
        skeptic_vote="skip",
        market_plan=plan,
        sizing_ok=True,
        sizing_reason="ok",
        guard=guard,
    )
    assert skipped.reason == "skeptic:skip"
    waiting = execution_gate(
        team_action="wait",
        skeptic_vote="buy",
        market_plan=plan,
        sizing_ok=True,
        sizing_reason="ok",
        guard=guard,
    )
    assert waiting.reason == "team_lead:wait"
    limited = execution_gate(
        team_action="buy",
        skeptic_vote="buy",
        market_plan=plan,
        sizing_ok=True,
        sizing_reason="ok",
        guard=GuardResult(False, "max_open_positions"),
    )
    assert limited.reason == "risk_guard:max_open_positions"
