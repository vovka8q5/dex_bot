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
    Venue,
    make_trading_client,
)
from dexbot.journal import Journal
from dexbot.llm import RoutingLLM
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
        llm = ScriptedLLM() if active.market.source == "mock" else RoutingLLM(active)
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
    sandbox = settings.mode != "mainnet"
    client = make_trading_client(
        settings.exchange.name,
        key,
        secret,
        sandbox=sandbox,
        allow_mainnet=settings.allow_mainnet,
    )
    label = f"{settings.exchange.name}-{'testnet' if sandbox else 'mainnet'}"
    return Venue(
        market=CcxtMarketData(client, quote=quote, timeframe=timeframe, limit=limit),
        broker=CcxtBroker(client, sandbox=sandbox, exchange_name=settings.exchange.name),
        name=label,
    )
