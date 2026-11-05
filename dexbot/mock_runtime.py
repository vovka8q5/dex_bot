"""Deterministic fixtures for ``--mock`` and the end-to-end test."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from dexbot.execution import FixtureMarket, PaperBroker, Venue
from dexbot.market import make_candles
from dexbot.models import Ticker


def _pair(
    base: str,
    *,
    last: float,
    volume: float,
    bid: float | None = None,
    ask: float | None = None,
    listed_at: datetime | None = None,
) -> Ticker:
    bid = last * 0.9999 if bid is None else bid
    ask = last * 1.0001 if ask is None else ask
    return Ticker(
        symbol=f"{base}/USDT",
        base=base,
        quote="USDT",
        last=last,
        bid=bid,
        ask=ask,
        quote_volume_24h=volume,
        listed_at=listed_at,
    )


def demo_fixtures(now: datetime | None = None) -> tuple[list[Ticker], dict]:
    moment = now or datetime.now(timezone.utc)
    old = moment - timedelta(days=400)
    fresh = moment - timedelta(days=1)
    tickers = [
        _pair("BTC", last=100.0, volume=80_000_000, listed_at=old),
        _pair("ETH", last=100.0, volume=70_000_000, listed_at=old),
        _pair("SOL", last=100.0, volume=60_000_000, listed_at=old),
        _pair("DOGE", last=100.0, volume=90_000_000, listed_at=old),
        _pair("ETH3L", last=100.0, volume=55_000_000, listed_at=old),
        _pair("NEW", last=100.0, volume=80_000_000, listed_at=fresh),
        _pair("WIDE", last=100.0, volume=80_000_000, bid=90.0, ask=110.0, listed_at=old),
        _pair("TINY", last=100.0, volume=1_000_000, listed_at=old),
    ]
    candles = {
        "BTC/USDT": make_candles(price=100.0, spike_last=True),
        "ETH/USDT": make_candles(price=100.0, spike_last=False),
        "SOL/USDT": make_candles(price=100.0, spike_last=False),
    }
    return tickers, candles


def demo_venue(cash: float, fee_rate: float) -> Venue:
    tickers, candles = demo_fixtures()
    return Venue(
        market=FixtureMarket(tickers, candles),
        broker=PaperBroker(cash=cash, fee_rate=fee_rate),
        name="paper",
    )


class ScriptedLLM:
    """Returns schema-valid JSON without calling a provider."""

    using_rules = False
    provider_name = "scripted"
    supports_live_search = False

    def usage_calls(self) -> list[dict]:
        return []

    def __init__(self) -> None:
        self.last_citations: list[str] = []

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
        del system, user, search, model
        payload: dict[str, Any] = dict(_DEFAULTS[agent])
        payload.update(_OVERRIDES.get((agent, symbol), {}))
        payload["symbol"] = symbol
        self.last_citations = ["https://example.com/x"] if agent == "news" and symbol.startswith("BTC") else []
        return schema.model_validate(payload)


_DEFAULTS: dict[str, dict] = {
    "base_analysis": {
        "rating": "buy",
        "bias": "long",
        "summary": "Scripted base case leans long on the supplied trend.",
        "technical": "Price is flat versus the 20-bar mean in the fixture.",
        "sentiment": "No social feed in the fixture.",
        "news": "No headline in the base pass.",
        "market_structure": "Liquid USDT book in the fixture.",
        "stance": "buy",
    },
    "volume": {
        "flagged": False,
        "volume_ratio": 1.0,
        "zscore": 0.0,
        "stance": "wait",
        "reasoning": "Scripted volume comment. Code replaces the flag.",
    },
    "news": {
        "items": [],
        "overall": "none",
        "stance": "wait",
        "reasoning": "Scripted news found nothing.",
    },
    "planner": {
        "side": "buy",
        "entry": 100.0,
        "stop_loss": 95.0,
        "take_profit": 110.0,
        "stance": "buy",
        "reasoning": "Scripted plan risks 5 to make 10.",
    },
    "risk_manager": {
        "approve": True,
        "reward_risk": 2.0,
        "min_reward_risk": 2.0,
        "stance": "buy",
        "reasoning": "Scripted risk manager agrees with the ratio.",
    },
    "skeptic": {
        "vote": "buy",
        "concerns": [],
        "reasoning": "Scripted skeptic does not veto.",
    },
    "team_lead": {
        "action": "buy",
        "confidence": 0.72,
        "entry": 100.0,
        "stop_loss": 95.0,
        "take_profit": 110.0,
        "reasoning": "Scripted team lead buys the planned long.",
        "dissent": "None in the fixture.",
    },
}

_OVERRIDES: dict[tuple[str, str], dict] = {
    ("news", "BTC/USDT"): {
        "items": [
            {
                "headline": "Spot ETF inflow print",
                "source": "web",
                "url": "https://example.com/etf",
                "label": "serious news",
                "summary": "Fixture headline used only by the mock desk.",
            }
        ],
        "overall": "serious news",
        "stance": "buy",
        "reasoning": "Scripted serious headline.",
    },
    ("skeptic", "ETH/USDT"): {
        "vote": "skip",
        "concerns": ["move has no catalyst in the fixture"],
        "reasoning": "Scripted skeptic skips ETH.",
    },
    ("planner", "SOL/USDT"): {
        "side": "buy",
        "entry": 100.0,
        "stop_loss": 98.0,
        "take_profit": 101.0,
        "stance": "buy",
        "reasoning": "Scripted plan with reward below the minimum.",
    },
    ("team_lead", "SOL/USDT"): {
        "action": "buy",
        "confidence": 0.4,
        "entry": 100.0,
        "stop_loss": 98.0,
        "take_profit": 101.0,
        "reasoning": "Scripted team lead still says buy. Code must reject the ratio.",
        "dissent": "Reward does not cover the stop.",
    },
}
