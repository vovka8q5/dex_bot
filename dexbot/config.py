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


class TokenPrice(Strict):
    input: float
    output: float


def _deepseek_models() -> ModelsConfig:
    return ModelsConfig(
        base_analysis="deepseek-chat",
        volume="deepseek-chat",
        news="deepseek-chat",
        planner="deepseek-chat",
        risk_manager="deepseek-chat",
        skeptic="deepseek-chat",
        team_lead="deepseek-chat",
    )


def _default_prices() -> dict[str, TokenPrice]:
    # Published list prices, USD per 1M tokens. Override in YAML when they change.
    return {
        "deepseek-chat": TokenPrice(input=0.27, output=1.10),
        "deepseek-reasoner": TokenPrice(input=0.55, output=2.19),
        "grok-4-fast": TokenPrice(input=0.20, output=0.50),
        "grok-4": TokenPrice(input=3.0, output=15.0),
    }


class FeedConfig(Strict):
    name: str
    url: str


def _default_feeds() -> list[FeedConfig]:
    return [
        FeedConfig(name="coindesk", url="https://www.coindesk.com/arc/outboundfeeds/rss/"),
        FeedConfig(name="cointelegraph", url="https://cointelegraph.com/rss"),
        FeedConfig(name="decrypt", url="https://decrypt.co/feed"),
        FeedConfig(name="theblock", url="https://www.theblock.co/rss.xml"),
    ]


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
    # auto: xAI if its key passes GET /models, else DeepSeek, else rules.
    provider: Literal["auto", "xai", "deepseek"] = "auto"
    base_url: str = "https://api.x.ai/v1"
    api_key_env: str = "XAI_API_KEY"
    deepseek_base_url: str = "https://api.deepseek.com"
    deepseek_api_key_env: str = "DEEPSEEK_API_KEY"
    timeout_s: float = 90
    temperature: float = 0.2
    models: ModelsConfig = Field(default_factory=ModelsConfig)
    deepseek_models: ModelsConfig = Field(default_factory=_deepseek_models)
    prices_per_million: dict[str, TokenPrice] = Field(default_factory=_default_prices)
    fallback: FallbackConfig = Field(default_factory=FallbackConfig)

    def model_for(self, agent: str, provider: str = "xai") -> str:
        table = self.deepseek_models if provider == "deepseek" else self.models
        return getattr(table, agent)


class NewsConfig(Strict):
    lookback_hours: int = 48
    timeout_s: float = 10
    feeds: list[FeedConfig] = Field(default_factory=_default_feeds)

    @field_validator("lookback_hours")
    @classmethod
    def _lookback(cls, value: int) -> int:
        if value < 1:
            raise ValueError("news.lookback_hours must be >= 1")
        return value


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
    # None means "derive it": demo while the desk is paper, otherwise the desk mode.
    # Demo is Bybit mainnet demo trading (https://api-demo.bybit.com), not testnet.
    mode: Literal["demo", "testnet", "mainnet"] | None = None


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
    mode: Literal["paper", "demo", "testnet", "mainnet"] = "paper"
    # Explicit flag. Mainnet orders are impossible while this stays false.
    # Demo trading does not use this flag and must not reach api.bybit.com.
    allow_mainnet: bool = False
    exchange: ExchangeConfig = Field(default_factory=ExchangeConfig)
    filter: FilterConfig = Field(default_factory=FilterConfig)
    risk: RiskConfig = Field(default_factory=RiskConfig)
    llm: LLMConfig = Field(default_factory=LLMConfig)
    news: NewsConfig = Field(default_factory=NewsConfig)
    tradingagents: TradingAgentsConfig = Field(default_factory=TradingAgentsConfig)
    scheduler: SchedulerConfig = Field(default_factory=SchedulerConfig)
    journal: JournalConfig = Field(default_factory=JournalConfig)
    volume: VolumeConfig = Field(default_factory=VolumeConfig)
    market: MarketConfig = Field(default_factory=MarketConfig)
    log_level: str = "INFO"
    log_json: bool = True

    @model_validator(mode="after")
    def _align_venue(self) -> Settings:
        if self.exchange.mode is None:
            self.exchange.mode = self.mode if self.mode in {"demo", "testnet", "mainnet"} else "demo"
        if self.exchange.mode == "demo" and self.exchange.name != "bybit":
            raise ValueError("exchange.mode demo is Bybit Demo Trading only")
        if self.mode == "paper":
            return self
        if self.exchange.mode != self.mode:
            raise ValueError(
                f"mode {self.mode} does not match exchange.mode {self.exchange.mode}. "
                "Set both to demo, testnet, or mainnet."
            )
        if self.mode == "demo" and self.exchange.name != "bybit":
            raise ValueError("mode demo is Bybit Demo Trading only (api-demo.bybit.com)")
        if self.mode == "mainnet" and not self.allow_mainnet:
            raise ValueError(
                "mode mainnet is refused unless allow_mainnet is true. "
                "Paper is the default; demo and testnet do not use this flag."
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


def _dotenv_candidates(config_path: Path) -> list[Path]:
    """`.env` next to the working directory, then next to the project that holds the config."""
    found: list[Path] = []
    cwd_env = Path.cwd() / ".env"
    if cwd_env.is_file():
        found.append(cwd_env)
    # config/default.yaml -> project root .env. Works for C:\dexbot\config\default.yaml too.
    if config_path.parent.name == "config":
        project_env = config_path.resolve().parents[1] / ".env"
        if project_env.is_file() and project_env not in found:
            found.append(project_env)
    return found


def load_dotenv_files(config_path: Path) -> None:
    """Load `.env` as UTF-8, including a Notepad BOM. Existing process env wins."""
    for candidate in _dotenv_candidates(config_path):
        load_dotenv(candidate, override=False, encoding="utf-8-sig")
    load_dotenv(override=False, encoding="utf-8-sig")


def load_settings(path: Path | str | None = None) -> Settings:
    config_path = Path(path) if path else default_config_path()
    load_dotenv_files(config_path)
    data = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise ValueError(f"config must be a mapping: {config_path}")
    mode = os.getenv("DEXBOT_MODE")
    if mode:
        data["mode"] = mode.strip().lower()
    flag = _env_bool("DEXBOT_ALLOW_MAINNET")
    if flag is not None:
        data["allow_mainnet"] = flag
    journal = os.getenv("DEXBOT_JOURNAL", "").strip()
    if journal:
        current = data.get("journal")
        if current is None:
            data["journal"] = {"path": journal}
        elif isinstance(current, dict):
            current["path"] = journal
        else:
            raise ValueError("journal in config must be a mapping when DEXBOT_JOURNAL is set")
    kill_file = os.getenv("DEXBOT_KILL_FILE", "").strip()
    if kill_file:
        current = data.get("risk")
        if current is None:
            data["risk"] = {"kill_switch_file": kill_file}
        elif isinstance(current, dict):
            current["kill_switch_file"] = kill_file
        else:
            raise ValueError("risk in config must be a mapping when DEXBOT_KILL_FILE is set")
    return Settings.model_validate(data)


def credential_env_names(name: str, kind: str) -> tuple[str, str]:
    """Env var names for a private venue. The values themselves are never logged."""
    if kind == "demo":
        if name != "bybit":
            raise ValueError("demo credentials are Bybit only")
        return "BYBIT_DEMO_API_KEY", "BYBIT_DEMO_API_SECRET"
    if kind == "testnet":
        if name == "bybit":
            return "BYBIT_TESTNET_API_KEY", "BYBIT_TESTNET_API_SECRET"
        if name == "binance":
            return "BINANCE_TESTNET_API_KEY", "BINANCE_TESTNET_API_SECRET"
        raise ValueError(f"unsupported exchange {name}")
    if kind == "mainnet":
        return "DEXBOT_MAINNET_API_KEY", "DEXBOT_MAINNET_API_SECRET"
    raise ValueError(f"unsupported venue {kind}")


def keys_are_set(name: str, kind: str) -> bool:
    """True when both env vars are non-empty. Does not return the secret."""
    key_env, secret_env = credential_env_names(name, kind)
    return bool(os.getenv(key_env, "").strip()) and bool(os.getenv(secret_env, "").strip())


def private_kind(settings: Settings) -> str:
    """Venue used for private calls.

    Paper desk mode still reports ``exchange.mode`` so ``check`` can probe the
    demo key without sending orders. ``run`` only uses this when mode is not paper.
    """
    if settings.mode == "paper":
        return settings.exchange.mode or "demo"
    return settings.mode


def exchange_credentials(settings: Settings) -> tuple[str, str]:
    """Return the key pair for the selected mode. Paper returns empty strings."""
    if settings.mode == "paper":
        return "", ""
    key_env, secret_env = credential_env_names(settings.exchange.name, settings.mode)
    key = os.getenv(key_env, "").strip()
    secret = os.getenv(secret_env, "").strip()
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
