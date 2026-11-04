"""Domain objects shared by the filter, agents, and journal."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class LLMModel(BaseModel):
    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)


def _lower(value: object) -> object:
    if isinstance(value, str):
        return value.strip().lower()
    return value


@dataclass
class Ticker:
    symbol: str
    base: str
    quote: str
    last: float
    bid: float
    ask: float
    quote_volume_24h: float
    spot: bool = True
    active: bool = True
    listed_at: datetime | None = None


@dataclass
class Candle:
    ts: int
    open: float
    high: float
    low: float
    close: float
    volume: float


@dataclass
class MarketStats:
    last: float
    ret_24h: float | None
    rsi_14: float | None
    atr_14: float | None
    sma_20: float | None
    volume_ratio: float | None
    volume_zscore: float | None
    last_volume: float | None
    median_volume: float | None
    candle_count: int
    volume_flagged: bool


@dataclass
class Fill:
    order_id: str
    symbol: str
    side: str
    price: float
    qty_base: float
    qty_quote: float
    fee_quote: float
    status: str


@dataclass
class Position:
    id: int
    symbol: str
    qty_base: float
    avg_entry: float
    stop_loss: float
    take_profit: float
    buy_fee: float
    opened_at: str
    entry_run_id: int
    last_mark: float
    status: str
    realized_pnl: float = 0.0
    closed_at: str | None = None


class BaseAnalysis(LLMModel):
    symbol: str = ""
    source: Literal["tradingagents", "crypto_native"] = "crypto_native"
    rating: Literal["buy", "overweight", "hold", "underweight", "sell", "review"]
    bias: Literal["long", "flat", "avoid"] = "flat"
    summary: str
    technical: str = ""
    sentiment: str = ""
    news: str = ""
    market_structure: str = ""
    stance: Literal["buy", "wait", "skip"] = "wait"
    raw_signal: str = ""

    @field_validator("rating", "bias", "stance", "source", mode="before")
    @classmethod
    def _norm(cls, value: object) -> object:
        return _lower(value)


class VolumeVerdict(LLMModel):
    symbol: str = ""
    flagged: bool
    volume_ratio: float = 0.0
    zscore: float = 0.0
    stance: Literal["buy", "wait", "skip"] = "wait"
    reasoning: str

    @field_validator("stance", mode="before")
    @classmethod
    def _norm(cls, value: object) -> object:
        return _lower(value)


class NewsItem(LLMModel):
    headline: str
    source: Literal["web", "x", "unknown"] = "unknown"
    url: str | None = None
    label: Literal["serious news", "minor news", "rumor"]
    summary: str

    @field_validator("source", "label", mode="before")
    @classmethod
    def _norm(cls, value: object) -> object:
        return _lower(value)


class NewsReport(LLMModel):
    symbol: str = ""
    items: list[NewsItem] = Field(default_factory=list)
    overall: Literal["serious news", "minor news", "rumor", "none"] = "none"
    stance: Literal["buy", "wait", "skip"] = "wait"
    reasoning: str
    citations: list[str] = Field(default_factory=list)

    @field_validator("overall", "stance", mode="before")
    @classmethod
    def _norm(cls, value: object) -> object:
        return _lower(value)


class Plan(LLMModel):
    symbol: str = ""
    side: Literal["buy", "flat"] = "flat"
    entry: float | None = None
    stop_loss: float | None = None
    take_profit: float | None = None
    stance: Literal["buy", "wait", "skip"] = "skip"
    reasoning: str
    valid: bool = False
    invalid_reason: str = ""

    @field_validator("side", "stance", mode="before")
    @classmethod
    def _norm(cls, value: object) -> object:
        return _lower(value)


class RiskVote(LLMModel):
    symbol: str = ""
    approve: bool
    reward_risk: float | None = None
    min_reward_risk: float
    stance: Literal["buy", "wait", "skip"] = "skip"
    reasoning: str
    code_approved: bool = False
    llm_approved: bool = False

    @field_validator("stance", mode="before")
    @classmethod
    def _norm(cls, value: object) -> object:
        return _lower(value)


class SkepticVote(LLMModel):
    symbol: str = ""
    vote: Literal["buy", "wait", "skip"]
    concerns: list[str] = Field(default_factory=list)
    reasoning: str

    @field_validator("vote", mode="before")
    @classmethod
    def _norm(cls, value: object) -> object:
        return _lower(value)

    @field_validator("concerns", mode="before")
    @classmethod
    def _concerns(cls, value: object) -> object:
        if value is None:
            return []
        if isinstance(value, str):
            return [value]
        return value


class TeamDecision(LLMModel):
    symbol: str = ""
    action: Literal["buy", "wait", "skip"]
    confidence: float = Field(ge=0, le=1)
    entry: float | None = None
    stop_loss: float | None = None
    take_profit: float | None = None
    reasoning: str
    dissent: str = ""

    @field_validator("action", mode="before")
    @classmethod
    def _norm(cls, value: object) -> object:
        return _lower(value)

    @model_validator(mode="after")
    def _buy_has_prices(self) -> TeamDecision:
        if self.action != "buy":
            return self
        if self.entry is None or self.stop_loss is None or self.take_profit is None:
            raise ValueError("buy requires entry, stop_loss and take_profit")
        if not (self.stop_loss < self.entry < self.take_profit):
            raise ValueError("require stop_loss < entry < take_profit")
        return self


@dataclass
class Candidate:
    ticker: Ticker
    spread_bps: float | None
    passed: bool
    reason: str | None


@dataclass
class CycleSummary:
    run_id: int
    mode: str
    accepted: int
    rejected: int
    executed: int
    blocked: int
    closed: int
    status: str

    def line(self) -> str:
        return (
            f"cycle run={self.run_id} mode={self.mode} status={self.status} "
            f"accepted={self.accepted} rejected={self.rejected} "
            f"executed={self.executed} blocked={self.blocked} closed={self.closed}"
        )


@dataclass
class SecondLayer:
    volume: VolumeVerdict
    news: NewsReport
    plan: Plan
    risk: RiskVote
    skeptic: SkepticVote
    decision: TeamDecision
    notes: list[str] = field(default_factory=list)
