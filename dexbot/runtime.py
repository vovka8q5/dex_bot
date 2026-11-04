"""Wire config to a venue, a journal, and an analyzer."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from dexbot.base_analysis import build_analyzer
from dexbot.config import Settings, exchange_credentials
from dexbot.execution import (
    CcxtBroker,
    CcxtMarketData,
    PaperBroker,
    PublicMarketData,
    SwitchingMarketData,
    Venue,
    make_public_client,
    make_trading_client,
)
from dexbot.journal import Journal
from dexbot.llm import build_llm
from dexbot.mock_runtime import ScriptedLLM, demo_venue


@dataclass
class Runtime:
    settings: Settings
    venue: Venue
    llm: Any
    journal: Journal
    analyzer: Any
    mock: bool


def build_runtime(settings: Settings, *, mock: bool = False, llm: Any = None) -> Runtime:
    active = settings.model_copy(deep=True)
    if mock:
        # Mock must not inherit a testnet or mainnet mode from the file.
        active.mode = "paper"
        active.market.source = "mock"
        active.tradingagents.enabled = False
    journal = Journal(active.journal.path)
    venue = _build_venue(active, journal)
    if llm is None:
        llm = ScriptedLLM() if active.market.source == "mock" else build_llm(active)
    return Runtime(
        settings=active,
        venue=venue,
        llm=llm,
        journal=journal,
        analyzer=build_analyzer(active, llm),
        mock=active.market.source == "mock",
    )


def _build_venue(settings: Settings, journal: Journal) -> Venue:
    if settings.mode == "mainnet" and not settings.allow_mainnet:
        from dexbot.execution import MainnetDisabled

        raise MainnetDisabled("allow_mainnet is false")
    quote = settings.filter.quote
    timeframe = settings.market.candle_timeframe
    limit = settings.market.candle_limit
    if settings.mode == "paper" and settings.market.source == "mock":
        cash = journal.ensure_cash(settings.risk.paper_equity_usd)
        return demo_venue(cash, settings.risk.fee_rate)
    if settings.mode == "paper":
        cash = journal.ensure_cash(settings.risk.paper_equity_usd)
        return Venue(
            market=PublicMarketData(settings.exchange.name, quote=quote, timeframe=timeframe, limit=limit),
            broker=PaperBroker(cash, settings.risk.fee_rate),
            name="paper",
        )
    key, secret = exchange_credentials(settings)
    if settings.mode == "demo":
        private = make_trading_client(
            "bybit",
            key,
            secret,
            kind="demo",
            allow_mainnet=False,
        )
        probe = make_public_client("bybit", demo=True)
        main_public = PublicMarketData("bybit", quote=quote, timeframe=timeframe, limit=limit)
        return Venue(
            market=SwitchingMarketData(
                CcxtMarketData(probe, quote=quote, timeframe=timeframe, limit=limit),
                main_public,
            ),
            broker=CcxtBroker(private, kind="demo", exchange_name="bybit", allow_mainnet=False),
            name="bybit-demo",
        )
    client = make_trading_client(
        settings.exchange.name,
        key,
        secret,
        kind=settings.mode,
        allow_mainnet=settings.allow_mainnet,
    )
    return Venue(
        market=CcxtMarketData(client, quote=quote, timeframe=timeframe, limit=limit),
        broker=CcxtBroker(
            client,
            kind=settings.mode,
            exchange_name=settings.exchange.name,
            allow_mainnet=settings.allow_mainnet,
        ),
        name=f"{settings.exchange.name}-{settings.mode}",
    )
