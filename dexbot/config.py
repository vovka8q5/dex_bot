"""YAML configuration. Secrets stay in the environment."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Literal

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

PREFILTER_AGENTS = frozenset({"volume"})


def _default_blocklist() -> list[str]:
    from dexbot.filter import DEFAULT_BLOCKLIST

    return list(DEFAULT_BLOCKLIST)


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class FilterConfig(Strict):
    quote: str = "USDT"
    top_n: int = 40
    top_n_min: int = 30
    top_n_max: int = 50
    min_quote_volume_usd: float = 20_000_000
    max_spread_bps: float = 15
    min_listing_age_days: int = 30
    missing_listing_policy: Literal["allow", "reject"] = "allow"
    blocklist: list[str] = Field(default_factory=_default_blocklist)
    prepool_multiplier: int = 3

    @model_validator(mode="after")
    def _top_n(self) -> FilterConfig:
        if self.top_n_min < 1 or self.top_n_min > self.top_n_max:
            raise ValueError("top_n_min must be >= 1 and <= top_n_max")
        if not self.top_n_min <= self.top_n <= self.top_n_max:
            raise ValueError("top_n must sit between top_n_min and top_n_max")
        if self.min_quote_volume_usd < 0 or self.max_spread_bps < 0:
            raise ValueError("volume and spread limits cannot be negative")
        if self.min_listing_age_days < 0:
            raise ValueError("min_listing_age_days cannot be negative")
        return self


class RiskConfig(Strict):
    max_equity_risk_pct: float = 0.01
    max_open_positions: int = 3
    max_daily_loss_pct: float = 0.03
    min_reward_risk: float = 2.0
    kill_switch: bool = False
    kill_switch_file: str = "data/KILL"
    paper_equity_usd: float = 10_000
    fee_rate: float = 0.001
    max_entry_deviation_pct: float = 0.02

    @field_validator("max_equity_risk_pct", "max_daily_loss_pct")
    @classmethod
    def _pct(cls, value: float) -> float:
        if not 0 < value <= 1:
            raise ValueError("percent limits must be within (0, 1]")
        return value

    @model_validator(mode="after")
    def _bounds(self) -> RiskConfig:
        if self.max_open_positions < 1:
            raise ValueError("max_open_positions must be >= 1")
        if self.min_reward_risk <= 0:
            raise ValueError("min_reward_risk must be > 0")
        if self.paper_equity_usd <= 0:
            raise ValueError("paper_equity_usd must be > 0")
        if not 0 <= self.fee_rate < 1:
            raise ValueError("fee_rate must be within [0, 1)")
        if self.max_entry_deviation_pct < 0:
            raise ValueError("max_entry_deviation_pct cannot be negative")
        return self


class ModelsConfig(Strict):
    base_analysis: str = "grok-4-fast"
    volume: str = "grok-4-fast"
    news: str = "grok-4-fast"
    planner: str = "grok-4-fast"
    risk_manager: str = "grok-4-fast"
    skeptic: str = "grok-4"
    team_lead: str = "grok-4"


class FallbackConfig(Strict):
    enabled: bool = False
    base_url: str = "http://127.0.0.1:8080/v1"
    api_key_env: str = "FALLBACK_API_KEY"
    model: str = "gpt-4o-mini"
    agents: list[str] = Field(default_factory=lambda: ["volume"])

    @model_validator(mode="after")
    def _prefilter_only(self) -> FallbackConfig:
        unknown = [name for name in self.agents if name not in PREFILTER_AGENTS]
        if unknown:
            allowed = ", ".join(sorted(PREFILTER_AGENTS))
            raise ValueError(
                "llm.fallback is only for cheap pre-filtering "
                f"(allowed: {allowed}); refused: {', '.join(unknown)}"
            )
        return self


class LLMConfig(Strict):
    provider: str = "xai"
    base_url: str = "https://api.x.ai/v1"
    api_key_env: str = "XAI_API_KEY"
    timeout_s: float = 90
    temperature: float = 0.2
    models: ModelsConfig = Field(default_factory=ModelsConfig)
    fallback: FallbackConfig = Field(default_factory=FallbackConfig)

    def model_for(self, agent: str) -> str:
        return getattr(self.models, agent)


class TradingAgentsConfig(Strict):
    enabled: bool = False
    fallback_to_native: bool = True
    llm_provider: str = "xai"
    quick_think_llm: str = "grok-4-fast"
    deep_think_llm: str = "grok-4"
    max_debate_rounds: int = 1
    max_risk_discuss_rounds: int = 1
    max_symbols: int = 5


class ExchangeConfig(Strict):
    name: Literal["bybit", "binance"] = "bybit"


class SchedulerConfig(Strict):
    interval_seconds: int = 3600
    align_to_hour: bool = True

    @field_validator("interval_seconds")
    @classmethod
    def _interval(cls, value: int) -> int:
        if value < 60:
            raise ValueError("interval_seconds must be >= 60")
        return value


class JournalConfig(Strict):
    path: str = "data/journal.sqlite"


class VolumeConfig(Strict):
    lookback: int = 24
    ratio_threshold: float = 2.0
    zscore_threshold: float = 2.0

    @model_validator(mode="after")
    def _bounds(self) -> VolumeConfig:
        if self.lookback < 5:
            raise ValueError("volume lookback must be >= 5")
        if self.ratio_threshold <= 0 or self.zscore_threshold <= 0:
            raise ValueError("volume thresholds must be > 0")
        return self


class MarketConfig(Strict):
    candle_timeframe: str = "1h"
    candle_limit: int = 120
    # public: paper mode reads exchange public data and never sends orders.
    # mock: fixtures, no network.
    source: Literal["public", "mock"] = "public"


class Settings(Strict):
    mode: Literal["paper", "testnet", "mainnet"] = "paper"
    # Explicit flag. Mainnet orders are impossible while this stays false.
    allow_mainnet: bool = False
    exchange: ExchangeConfig = Field(default_factory=ExchangeConfig)
    filter: FilterConfig = Field(default_factory=FilterConfig)
    risk: RiskConfig = Field(default_factory=RiskConfig)
    llm: LLMConfig = Field(default_factory=LLMConfig)
    tradingagents: TradingAgentsConfig = Field(default_factory=TradingAgentsConfig)
    scheduler: SchedulerConfig = Field(default_factory=SchedulerConfig)
    journal: JournalConfig = Field(default_factory=JournalConfig)
    volume: VolumeConfig = Field(default_factory=VolumeConfig)
    market: MarketConfig = Field(default_factory=MarketConfig)
    log_level: str = "INFO"
    log_json: bool = True

    @model_validator(mode="after")
    def _mainnet_flag(self) -> Settings:
        if self.mode == "mainnet" and not self.allow_mainnet:
            raise ValueError(
                "mode mainnet is refused unless allow_mainnet is true. "
                "Paper is the default; testnet is opt-in via mode: testnet."
            )
        return self


def package_config_path() -> Path:
    return Path(__file__).resolve().parents[1] / "config" / "default.yaml"


def default_config_path() -> Path:
    cwd = Path.cwd() / "config" / "default.yaml"
    if cwd.exists():
        return cwd
    packaged = package_config_path()
    if packaged.exists():
        return packaged
    raise FileNotFoundError("config/default.yaml not found")


def _env_bool(name: str) -> bool | None:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return None
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def load_settings(path: Path | str | None = None) -> Settings:
    load_dotenv(override=False)
    config_path = Path(path) if path else default_config_path()
    data = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise ValueError(f"config must be a mapping: {config_path}")
    mode = os.getenv("DEXBOT_MODE")
    if mode:
        data["mode"] = mode.strip().lower()
    flag = _env_bool("DEXBOT_ALLOW_MAINNET")
    if flag is not None:
        data["allow_mainnet"] = flag
    return Settings.model_validate(data)


def exchange_credentials(settings: Settings) -> tuple[str, str]:
    """Return the key pair for the selected mode. Paper returns empty strings."""
    if settings.mode == "paper":
        return "", ""
    if settings.mode == "testnet":
        if settings.exchange.name == "bybit":
            key_env, secret_env = "BYBIT_TESTNET_API_KEY", "BYBIT_TESTNET_API_SECRET"
        else:
            key_env, secret_env = "BINANCE_TESTNET_API_KEY", "BINANCE_TESTNET_API_SECRET"
    else:
        key_env, secret_env = "DEXBOT_MAINNET_API_KEY", "DEXBOT_MAINNET_API_SECRET"
    key = os.getenv(key_env, "")
    secret = os.getenv(secret_env, "")
    if not key or not secret:
        raise RuntimeError(
            f"mode {settings.mode} on {settings.exchange.name} requires {key_env} and {secret_env}"
        )
    return key, secret


def require_llm_key(settings: Settings) -> str:
    key = os.getenv(settings.llm.api_key_env, "")
    if not key:
        raise RuntimeError(
            f"missing {settings.llm.api_key_env} for LLM provider {settings.llm.provider}"
        )
    return key
