"""Second-layer agents. Numeric risk and the volume flag are overwritten in code."""

from __future__ import annotations

import json
import logging
from typing import Any

from dexbot.config import Settings
from dexbot.models import (
    BaseAnalysis,
    MarketStats,
    NewsReport,
    Plan,
    RiskVote,
    SecondLayer,
    SkepticVote,
    TeamDecision,
    VolumeVerdict,
)
from dexbot.risk import ratio_ok, reward_risk_ratio

logger = logging.getLogger("dexbot.agents")

_JSON = "Reply with one JSON object and no markdown."

_VOLUME_SYSTEM = f"""You comment on a volume flag that code already computed.
Do not change the numbers. Explain whether the spike looks like participation or a print.
Return JSON: symbol, flagged (bool), volume_ratio, zscore, stance (buy|wait|skip), reasoning.
{_JSON}"""

_NEWS_SYSTEM = f"""You search the web and X for why this crypto pair moved.
Label every item with exactly one of: "serious news", "minor news", "rumor".
source is "web", "x", or "unknown".
overall is one of those three labels, or "none" when nothing relevant turned up.
stance is buy, wait, or skip. A rumor-only tape is not a buy.
Return JSON: symbol, items (list of headline, source, url, label, summary), overall, stance, reasoning.
{_JSON}"""

_NEWS_RSS_SYSTEM = f"""You label crypto headlines already collected from RSS.
Do not invent headlines and do not browse. Use only the headlines list.
Label every item with exactly one of: "serious news", "minor news", or "rumor".
source is "web". url is the link. overall is one of those three, or "none" when the list is empty.
stance is buy, wait, or skip. A rumor-only tape is not a buy.
Return JSON: symbol, items (list of headline, source, url, label, summary), overall, stance, reasoning.
{_JSON}"""

_PLAN_SYSTEM = f"""You plan a spot long only. No shorts.
Propose entry, stop_loss, take_profit near the mark. stop_loss < entry < take_profit.
If there is no trade, set side to "flat" and stance to "skip".
Return JSON: symbol, side (buy|flat), entry, stop_loss, take_profit, stance, reasoning.
{_JSON}"""

_RISK_SYSTEM = f"""You are the risk manager. Reject a long whose reward:risk is below the minimum.
Potential loss is entry minus stop. Potential profit is take-profit minus entry.
approve true only when the ratio is at least min_reward_risk. Code will recompute the ratio
and can reject a trade you approve. Return JSON:
symbol, approve (bool), reward_risk, min_reward_risk, stance (buy|skip), reasoning.
{_JSON}"""

_SKEPTIC_SYSTEM = f"""You are the skeptic. Critique the plan. Vote buy, wait, or skip.
skip means the trade must not be sent. wait means you are unconvinced but you do not veto.
Prefer skip when the catalyst is a rumor, the volume flag is absent, or the stop is arbitrary.
Return JSON: symbol, vote (buy|wait|skip), concerns (list of strings), reasoning.
{_JSON}"""

_LEAD_SYSTEM = f"""You are the team lead. Read every agent and decide.
Return JSON only, with keys:
symbol, action (buy|wait|skip), confidence (0 to 1), entry, stop_loss, take_profit,
reasoning, dissent.
action buy requires stop_loss < entry < take_profit.
A skeptic vote of skip should normally become action skip. You do not choose size.
{_JSON}"""


def _user(payload: dict) -> str:
    return json.dumps(payload, ensure_ascii=False, default=str)


def _ask(llm: Any, agent: str, symbol: str, schema: type, system: str, payload: dict, *, search: bool = False):
    return llm.complete_json(
        agent=agent,
        symbol=symbol,
        schema=schema,
        system=system,
        user=_user(payload),
        search=search,
        model=None,
    )


def normalize_plan(plan: Plan, *, symbol: str, mark: float, max_deviation: float) -> Plan:
    if plan.side != "buy" or plan.entry is None or plan.stop_loss is None or plan.take_profit is None:
        return plan.model_copy(
            update={
                "symbol": symbol,
                "side": "flat",
                "stance": "skip",
                "valid": False,
                "invalid_reason": plan.invalid_reason or "no_trade",
            }
        )
    if not (plan.stop_loss < plan.entry < plan.take_profit):
        return plan.model_copy(
            update={"symbol": symbol, "side": "flat", "stance": "skip", "valid": False, "invalid_reason": "price_order"}
        )
    if mark <= 0 or abs(plan.entry - mark) / mark > max_deviation + 1e-12:
        return plan.model_copy(
            update={
                "symbol": symbol,
                "side": "flat",
                "stance": "skip",
                "valid": False,
                "invalid_reason": "entry_deviation",
            }
        )
    return plan.model_copy(update={"symbol": symbol, "stance": "buy", "valid": True, "invalid_reason": ""})


def _failed(symbol: str, text: str) -> str:
    return text[:500]


def run_second_layer(
    llm: Any,
    settings: Settings,
    *,
    symbol: str,
    stats: MarketStats,
    base: BaseAnalysis,
    mark: float,
    headlines: list[dict] | None = None,
) -> SecondLayer:
    context = {
        "symbol": symbol,
        "mark": mark,
        "stats": stats.__dict__,
        "base": base.model_dump(),
    }
    notes: list[str] = []

    try:
        volume = _ask(llm, "volume", symbol, VolumeVerdict, _VOLUME_SYSTEM, context)
        volume = volume.model_copy(
            update={
                "symbol": symbol,
                "flagged": bool(stats.volume_flagged),
                "volume_ratio": float(stats.volume_ratio or 0),
                "zscore": float(stats.volume_zscore or 0),
                "stance": "buy" if stats.volume_flagged else "wait",
            }
        )
    except Exception as exc:
        logger.exception("volume_agent_failed symbol=%s", symbol)
        notes.append(f"volume:{exc}")
        volume = VolumeVerdict(
            symbol=symbol,
            flagged=bool(stats.volume_flagged),
            volume_ratio=float(stats.volume_ratio or 0),
            zscore=float(stats.volume_zscore or 0),
            stance="buy" if stats.volume_flagged else "wait",
            reasoning=_failed(symbol, f"volume_failed: {exc}"),
        )

    live_search = bool(getattr(llm, "supports_live_search", False))
    if live_search:
        news_system = _NEWS_SYSTEM
        news_payload = {**context, "task": "Search the web and X for the last 24h move."}
    else:
        news_system = _NEWS_RSS_SYSTEM
        news_payload = {
            **context,
            "headlines": headlines or [],
            "task": "Label these RSS headlines from the lookback window.",
        }
    try:
        news = _ask(
            llm,
            "news",
            symbol,
            NewsReport,
            news_system,
            news_payload,
            search=live_search,
        )
        news = news.model_copy(
            update={"symbol": symbol, "citations": list(getattr(llm, "last_citations", []) or [])}
        )
    except Exception as exc:
        logger.exception("news_agent_failed symbol=%s", symbol)
        notes.append(f"news:{exc}")
        news = NewsReport(
            symbol=symbol,
            items=[],
            overall="none",
            stance="skip",
            reasoning=_failed(symbol, f"news_failed: {exc}"),
        )

    try:
        plan = normalize_plan(
            _ask(llm, "planner", symbol, Plan, _PLAN_SYSTEM, {**context, "volume": volume.model_dump(), "news": news.model_dump()}),
            symbol=symbol,
            mark=mark,
            max_deviation=settings.risk.max_entry_deviation_pct,
        )
    except Exception as exc:
        logger.exception("planner_failed symbol=%s", symbol)
        notes.append(f"planner:{exc}")
        plan = Plan(symbol=symbol, side="flat", stance="skip", reasoning=_failed(symbol, f"planner_failed: {exc}"), valid=False, invalid_reason="planner_failed")

    ratio = None
    if plan.entry is not None and plan.stop_loss is not None and plan.take_profit is not None:
        ratio = reward_risk_ratio(plan.entry, plan.stop_loss, plan.take_profit)
    code_approved = bool(plan.valid and plan.side == "buy" and ratio_ok(ratio, settings.risk.min_reward_risk))
    try:
        raw_risk = _ask(
            llm,
            "risk_manager",
            symbol,
            RiskVote,
            _RISK_SYSTEM,
            {
                "symbol": symbol,
                "plan": plan.model_dump(),
                "min_reward_risk": settings.risk.min_reward_risk,
                "code_ratio": ratio,
            },
        )
        llm_approved = bool(raw_risk.approve)
        approved = code_approved and llm_approved
        risk = raw_risk.model_copy(
            update={
                "symbol": symbol,
                "approve": approved,
                "reward_risk": ratio,
                "min_reward_risk": settings.risk.min_reward_risk,
                "code_approved": code_approved,
                "llm_approved": llm_approved,
                "stance": "buy" if approved else "skip",
            }
        )
    except Exception as exc:
        logger.exception("risk_manager_failed symbol=%s", symbol)
        notes.append(f"risk:{exc}")
        risk = RiskVote(
            symbol=symbol,
            approve=False,
            reward_risk=ratio,
            min_reward_risk=settings.risk.min_reward_risk,
            stance="skip",
            reasoning=_failed(symbol, f"risk_failed: {exc}"),
            code_approved=code_approved,
            llm_approved=False,
        )

    try:
        skeptic = _ask(
            llm,
            "skeptic",
            symbol,
            SkepticVote,
            _SKEPTIC_SYSTEM,
            {
                "symbol": symbol,
                "base": base.model_dump(),
                "volume": volume.model_dump(),
                "news": news.model_dump(),
                "plan": plan.model_dump(),
                "risk": risk.model_dump(),
            },
        )
        skeptic = skeptic.model_copy(update={"symbol": symbol})
    except Exception as exc:
        logger.exception("skeptic_failed symbol=%s", symbol)
        notes.append(f"skeptic:{exc}")
        skeptic = SkepticVote(
            symbol=symbol,
            vote="skip",
            concerns=["agent_failed"],
            reasoning=_failed(symbol, f"skeptic_failed: {exc}"),
        )

    try:
        decision = _ask(
            llm,
            "team_lead",
            symbol,
            TeamDecision,
            _LEAD_SYSTEM,
            {
                "symbol": symbol,
                "mark": mark,
                "base": base.model_dump(),
                "volume": volume.model_dump(),
                "news": news.model_dump(),
                "plan": plan.model_dump(),
                "risk": risk.model_dump(),
                "skeptic": skeptic.model_dump(),
            },
        )
        decision = decision.model_copy(update={"symbol": symbol})
    except Exception as exc:
        logger.exception("team_lead_failed symbol=%s", symbol)
        notes.append(f"team_lead:{exc}")
        decision = TeamDecision(
            symbol=symbol,
            action="skip",
            confidence=0,
            reasoning=_failed(symbol, f"team_lead_failed: {exc}"),
            dissent="agent_failed",
        )

    return SecondLayer(
        volume=volume,
        news=news,
        plan=plan,
        risk=risk,
        skeptic=skeptic,
        decision=decision,
        notes=notes,
    )
