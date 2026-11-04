from datetime import datetime, timezone
from pathlib import Path

import pytest
from pydantic import ValidationError

from dexbot.base_analysis import (
    analysis_from_signal,
    apply_ta_overrides,
    map_rating,
    to_ta_ticker,
    tradingagents_importable,
)
from dexbot.config import FallbackConfig, Settings, load_settings, package_config_path
from dexbot.execution import MainnetDisabled, assert_sandbox_urls, make_trading_client
from dexbot.schedule import sleep_seconds

ROOT = Path(__file__).resolve().parents[1]


def test_default_yaml_is_paper():
    settings = load_settings(package_config_path())
    assert settings.mode == "paper"
    assert settings.allow_mainnet is False
    assert settings.risk.max_equity_risk_pct == pytest.approx(0.01)
    assert settings.risk.max_open_positions == 3
    assert settings.risk.max_daily_loss_pct == pytest.approx(0.03)
    assert settings.risk.min_reward_risk == pytest.approx(2)
    assert settings.llm.base_url == "https://api.x.ai/v1"
    assert settings.llm.api_key_env == "XAI_API_KEY"
    assert settings.filter.top_n_min <= settings.filter.top_n <= settings.filter.top_n_max
    assert "DOGE" in {item.upper() for item in settings.filter.blocklist}
    assert settings.llm.models.skeptic
    assert settings.llm.models.team_lead


def test_mainnet_requires_named_flag():
    with pytest.raises(ValidationError):
        Settings(mode="mainnet", allow_mainnet=False)
    allowed = Settings(mode="mainnet", allow_mainnet=True)
    assert allowed.allow_mainnet is True


def test_fallback_cannot_host_skeptic_or_news():
    with pytest.raises(ValidationError):
        FallbackConfig(enabled=True, agents=["skeptic"])
    with pytest.raises(ValidationError):
        FallbackConfig(enabled=True, agents=["news"])
    assert FallbackConfig(enabled=True, agents=["volume"]).agents == ["volume"]


def test_tradingagents_mapping_without_the_package():
    assert to_ta_ticker("BTC/USDT") == "BTC-USD"
    assert to_ta_ticker("1000PEPE/USDT") == "1000PEPE-USD"
    assert map_rating("Buy") == ("buy", "long", "buy")
    assert map_rating("REVIEW")[2] == "wait"
    assert map_rating("nonsense")[0] == "review"
    analysis = analysis_from_signal(
        "BTC/USDT",
        "Overweight",
        {"market_report": "trend", "final_trade_decision": "lean long"},
    )
    assert analysis.source == "tradingagents"
    assert analysis.bias == "long"
    assert analysis.technical == "trend"
    cfg = apply_ta_overrides(
        {"llm_provider": "openai", "backend_url": None, "checkpoint_enabled": True},
        Settings(),
    )
    assert cfg["llm_provider"] == "xai"
    assert cfg["backend_url"] == "https://api.x.ai/v1"
    assert cfg["checkpoint_enabled"] is False
    # The import check must not raise when the optional package is absent.
    assert tradingagents_importable() in {True, False}


def test_mainnet_client_refused_and_sandbox_urls_point_at_testnet():
    with pytest.raises(MainnetDisabled):
        make_trading_client("bybit", "k", "s", sandbox=False, allow_mainnet=False)
    bybit = make_trading_client("bybit", "k", "s", sandbox=True, allow_mainnet=False)
    binance = make_trading_client("binance", "k", "s", sandbox=True, allow_mainnet=False)
    assert_sandbox_urls("bybit", bybit)
    assert_sandbox_urls("binance", binance)


def test_sleep_aligns_to_the_hour():
    moment = datetime(2026, 1, 1, 1, 15, tzinfo=timezone.utc).timestamp()
    assert sleep_seconds(moment, 3600, True) == pytest.approx(45 * 60)
    assert sleep_seconds(moment, 3600, False) == pytest.approx(3600)


def test_repo_config_path_matches_package():
    assert package_config_path() == ROOT / "config" / "default.yaml"
