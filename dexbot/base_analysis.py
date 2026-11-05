"""Base analysis: TradingAgents when installed, otherwise a crypto-native pass.

Direct use of TauricResearch/TradingAgents as the only analyst is a poor fit
for an hourly crypto desk:

* The package is a LangGraph equity-research stack (langchain, langgraph,
  yfinance, vendor keys, a pandas major pin). One ``propagate`` call is a
  long tool-and-debate loop, which does not fit 30-50 symbols an hour.
* Its data vendors are not the ccxt book we filter and trade, so a rating
  can refer to a different price than the testnet or paper fill.
* The upstream graph ends in its own simulated portfolio. Our hard risk
  guard and ccxt execution have to stay outside that graph.

``TradingAgentsAdapter`` calls ``TradingAgentsGraph.propagate(..., asset_type="crypto")``
when ``tradingagents.enabled`` is true and the package imports. The default
base layer is ``CryptoNativeAnalyzer``, which fills the same four-role report
(technical, sentiment, news, market structure) from our own 1h candles.
"""

from __future__ import annotations

import importlib.util
import logging
from typing import Any

from dexbot.config import Settings
from dexbot.models import BaseAnalysis, Candle, MarketStats

logger = logging.getLogger("dexbot.base")

_RATING_MAP = {
    "buy": ("buy", "long", "buy"),
    "overweight": ("overweight", "long", "buy"),
    "hold": ("hold", "flat", "wait"),
    "underweight": ("underweight", "avoid", "skip"),
    "sell": ("sell", "avoid", "skip"),
    "review": ("review", "flat", "wait"),
}


def tradingagents_importable() -> bool:
    try:
        return importlib.util.find_spec("tradingagents") is not None
    except (ImportError, ValueError):
        return False


def to_ta_ticker(symbol: str) -> str:
    """Map a ccxt spot symbol onto the Yahoo-style ticker TradingAgents expects."""
    base = symbol.split("/")[0].strip().upper()
    return f"{base}-USD"


def map_rating(signal: str | None) -> tuple[str, str, str]:
    key = (signal or "").strip().lower()
    return _RATING_MAP.get(key, ("review", "flat", "wait"))


def apply_ta_overrides(base: dict, settings: Settings) -> dict:
    """Point an upstream TradingAgents config at our xAI (or other) endpoint."""
    cfg = dict(base)
    ta = settings.tradingagents
    cfg["llm_provider"] = ta.llm_provider
    cfg["backend_url"] = settings.llm.base_url
    cfg["quick_think_llm"] = ta.quick_think_llm
    cfg["deep_think_llm"] = ta.deep_think_llm
    cfg["quick_think_provider"] = ta.llm_provider
    cfg["deep_think_provider"] = ta.llm_provider
    cfg["max_debate_rounds"] = ta.max_debate_rounds
    cfg["max_risk_discuss_rounds"] = ta.max_risk_discuss_rounds
    cfg["checkpoint_enabled"] = False
    cfg["temperature"] = settings.llm.temperature
    cfg["output_language"] = "English"
    return cfg


def _text(state: dict, *keys: str) -> str:
    for key in keys:
        value = state.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()[:4000]
    return ""


def analysis_from_signal(symbol: str, signal: str, state: dict | None) -> BaseAnalysis:
    rating, bias, stance = map_rating(signal)
    body = state or {}
    summary = _text(body, "final_trade_decision", "investment_plan", "trader_investment_plan") or str(signal)
    return BaseAnalysis(
        symbol=symbol,
        source="tradingagents",
        rating=rating,  # type: ignore[arg-type]
        bias=bias,  # type: ignore[arg-type]
        summary=summary[:4000],
        technical=_text(body, "market_report"),
        sentiment=_text(body, "sentiment_report", "social_report"),
        news=_text(body, "news_report"),
        market_structure=_text(body, "fundamentals_report"),
        stance=stance,  # type: ignore[arg-type]
        raw_signal=str(signal),
    )


_NATIVE_SYSTEM = """You are the base crypto analyst on a spot-only paper/testnet desk.
Use only the numbers in the user JSON. Do not invent prices, volume, or news.
Return one JSON object with keys:
symbol, rating (buy|overweight|hold|underweight|sell|review),
bias (long|flat|avoid), summary, technical, sentiment, news, market_structure,
stance (buy|wait|skip).
rating buy/overweight goes with stance buy and bias long.
hold/review goes with stance wait and bias flat.
underweight/sell goes with stance skip and bias avoid.
No markdown."""


class CryptoNativeAnalyzer:
    def __init__(self, llm: Any) -> None:
        self.llm = llm

    def analyze(self, symbol: str, stats: MarketStats, candles: list[Candle]) -> BaseAnalysis:
        import json

        payload = {
            "symbol": symbol,
            "last": stats.last,
            "return_24h": stats.ret_24h,
            "rsi_14": stats.rsi_14,
            "atr_14": stats.atr_14,
            "sma_20": stats.sma_20,
            "volume_ratio": stats.volume_ratio,
            "volume_zscore": stats.volume_zscore,
            "volume_flagged": stats.volume_flagged,
            "candle_count": stats.candle_count,
            "recent_closes": [candle.close for candle in candles[-12:]],
        }
        result = self.llm.complete_json(
            agent="base_analysis",
            symbol=symbol,
            schema=BaseAnalysis,
            system=_NATIVE_SYSTEM,
            user=json.dumps(payload, default=str),
            search=False,
            model=None,
        )
        _rating, bias, stance = map_rating(result.rating)
        return result.model_copy(
            update={
                "symbol": symbol,
                "source": "crypto_native",
                "bias": bias,
                "stance": stance,
                "raw_signal": result.rating,
            }
        )


class TradingAgentsAdapter:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._graph: Any = None

    def analyze(self, symbol: str, stats: MarketStats, candles: list[Candle]) -> BaseAnalysis:
        del stats, candles  # upstream loads its own series; the second layer uses ours
        graph = self._graph or self._build()
        self._graph = graph
        ticker = to_ta_ticker(symbol)
        from datetime import datetime, timezone

        day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        try:
            state, signal = graph.propagate(ticker, day, asset_type="crypto")
        except TypeError:
            state, signal = graph.propagate(ticker, day)
        if not isinstance(state, dict):
            state = {}
        return analysis_from_signal(symbol, str(signal), state)

    def _build(self) -> Any:
        from tradingagents.default_config import DEFAULT_CONFIG
        from tradingagents.graph.trading_graph import TradingAgentsGraph

        cfg = apply_ta_overrides(DEFAULT_CONFIG, self.settings)
        return TradingAgentsGraph(
            selected_analysts=("market", "social", "news", "fundamentals"),
            debug=False,
            config=cfg,
        )


class HybridAnalyzer:
    def __init__(self, native: CryptoNativeAnalyzer, adapter: TradingAgentsAdapter, max_symbols: int, fallback: bool) -> None:
        self.native = native
        self.adapter = adapter
        self.max_symbols = max_symbols
        self.fallback = fallback
        self._used = 0

    def analyze(self, symbol: str, stats: MarketStats, candles: list[Candle]) -> BaseAnalysis:
        if self._used < self.max_symbols:
            self._used += 1
            try:
                return self.adapter.analyze(symbol, stats, candles)
            except Exception:
                logger.exception("tradingagents_failed symbol=%s", symbol)
                if not self.fallback:
                    raise
        return self.native.analyze(symbol, stats, candles)


def build_analyzer(settings: Settings, llm: Any):
    native = CryptoNativeAnalyzer(llm)
    if not settings.tradingagents.enabled:
        return native
    if not tradingagents_importable():
        if settings.tradingagents.fallback_to_native:
            logger.warning("tradingagents is not installed; using the crypto-native base analyst")
            return native
        raise RuntimeError(
            "tradingagents.enabled is true but the package is not installed. "
            "Install with: pip install 'dexbot[tradingagents]'"
        )
    return HybridAnalyzer(
        native,
        TradingAgentsAdapter(settings),
        settings.tradingagents.max_symbols,
        settings.tradingagents.fallback_to_native,
    )
