"""Deterministic stand-in for the six agents when no LLM key is available.

Numbers come from the payload the pipeline already computed. News search is
skipped because that requires xAI. A buy still has to clear the code risk gate.
"""

from __future__ import annotations

import json

from dexbot.models import (
    BaseAnalysis,
    NewsReport,
    Plan,
    RiskVote,
    SkepticVote,
    TeamDecision,
    VolumeVerdict,
)
from dexbot.risk import ratio_ok, reward_risk_ratio


def _payload(user: str) -> dict:
    try:
        value = json.loads(user)
    except json.JSONDecodeError:
        return {}
    return value if isinstance(value, dict) else {}


def _mark(data: dict) -> float:
    if data.get("mark"):
        return float(data["mark"])
    stats = data.get("stats") or {}
    if stats.get("last"):
        return float(stats["last"])
    if data.get("last"):
        return float(data["last"])
    return 0.0


def _flagged(data: dict) -> bool:
    if "volume_flagged" in data:
        return bool(data["volume_flagged"])
    stats = data.get("stats") or {}
    if "volume_flagged" in stats:
        return bool(stats["volume_flagged"])
    volume = data.get("volume") or {}
    if "flagged" in volume:
        return bool(volume["flagged"])
    return False


def _prices(mark: float, atr: float | None) -> tuple[float, float, float]:
    distance = mark * 0.02
    if atr and atr > 0:
        distance = max(distance, min(2 * atr, mark * 0.2))
    distance = min(max(distance, mark * 0.01), mark * 0.2)
    return mark, mark - distance, mark + 2 * distance


class RuleBasedLLM:
    """Schema-valid agent replies with no network call."""

    using_rules = True
    provider_name = "rules"
    supports_live_search = False
    last_citations: list[str] = []

    def usage_calls(self) -> list[dict]:
        return []

    def complete_json(
        self,
        *,
        agent: str,
        symbol: str,
        schema: type,
        system: str,
        user: str,
        search: bool = False,
        model: str | None = None,
    ):
        del system, search, model
        data = _payload(user)
        self.last_citations = []
        built = _decide(agent, symbol, data)
        return schema.model_validate(built)


def _decide(agent: str, symbol: str, data: dict) -> dict:
    note = "rule fallback: no LLM call"
    if agent == "base_analysis":
        flagged = _flagged(data)
        return {
            "symbol": symbol,
            "rating": "buy" if flagged else "hold",
            "bias": "long" if flagged else "flat",
            "summary": note + ("; volume is elevated" if flagged else "; volume is ordinary"),
            "technical": "Rule base uses the candle stats already in the prompt.",
            "sentiment": "No social feed without an LLM.",
            "news": "News search is off in rule fallback.",
            "market_structure": "Spot USDT book.",
            "stance": "buy" if flagged else "wait",
        }
    if agent == "volume":
        stats = data.get("stats") or {}
        flagged = _flagged(data)
        return {
            "symbol": symbol,
            "flagged": flagged,
            "volume_ratio": float(stats.get("volume_ratio") or 0),
            "zscore": float(stats.get("volume_zscore") or 0),
            "stance": "buy" if flagged else "wait",
            "reasoning": note + "; flag is computed in code",
        }
    if agent == "news":
        return {
            "symbol": symbol,
            "items": [],
            "overall": "none",
            "stance": "wait",
            "reasoning": note + "; web and X search require xAI",
            "citations": [],
        }
    if agent == "planner":
        mark = _mark(data)
        stats = data.get("stats") or {}
        atr = stats.get("atr_14")
        if not _flagged(data) or mark <= 0:
            return {
                "symbol": symbol,
                "side": "flat",
                "stance": "skip",
                "reasoning": note + "; no volume flag",
                "valid": False,
                "invalid_reason": "no_trade",
            }
        entry, stop, take = _prices(mark, float(atr) if atr else None)
        return {
            "symbol": symbol,
            "side": "buy",
            "entry": entry,
            "stop_loss": stop,
            "take_profit": take,
            "stance": "buy",
            "reasoning": note + "; stop is two ATRs or 2% and take-profit is twice that",
            "valid": True,
        }
    if agent == "risk_manager":
        plan = data.get("plan") or {}
        minimum = float(data.get("min_reward_risk") or 2)
        ratio = data.get("code_ratio")
        if ratio is None and plan.get("entry") and plan.get("stop_loss") and plan.get("take_profit"):
            ratio = reward_risk_ratio(float(plan["entry"]), float(plan["stop_loss"]), float(plan["take_profit"]))
        approved = bool(plan.get("side") == "buy" and plan.get("valid", True) and ratio_ok(
            float(ratio) if ratio is not None else None, minimum
        ))
        return {
            "symbol": symbol,
            "approve": approved,
            "reward_risk": ratio,
            "min_reward_risk": minimum,
            "stance": "buy" if approved else "skip",
            "reasoning": note + f"; ratio={ratio}",
            "code_approved": approved,
            "llm_approved": approved,
        }
    if agent == "skeptic":
        plan = data.get("plan") or {}
        risk = data.get("risk") or {}
        vote = "buy" if _flagged(data) and plan.get("valid") and risk.get("approve") else "skip"
        return {
            "symbol": symbol,
            "vote": vote,
            "concerns": [] if vote == "buy" else ["rule fallback will not buy a quiet or rejected plan"],
            "reasoning": note,
        }
    if agent == "team_lead":
        plan = data.get("plan") or {}
        skeptic = (data.get("skeptic") or {}).get("vote")
        approved = bool((data.get("risk") or {}).get("approve"))
        if skeptic == "buy" and approved and _flagged(data) and plan.get("entry"):
            return {
                "symbol": symbol,
                "action": "buy",
                "confidence": 0.55,
                "entry": plan.get("entry"),
                "stop_loss": plan.get("stop_loss"),
                "take_profit": plan.get("take_profit"),
                "reasoning": note + "; volume flag, reward:risk, and skeptic agree",
                "dissent": "News search did not run.",
            }
        return {
            "symbol": symbol,
            "action": "skip",
            "confidence": 0.2,
            "reasoning": note + "; missing volume, risk approval, or skeptic buy",
            "dissent": "rule fallback",
        }
    raise ValueError(f"no rule for agent {agent}")
