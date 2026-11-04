import json

import pytest
from pydantic import ValidationError

from dexbot.agents import run_second_layer
from dexbot.check import inspect_connection, parse_key_permissions, render_check
from dexbot.cli import main
from dexbot.config import (
    ExchangeConfig,
    Settings,
    VolumeConfig,
    load_settings,
    package_config_path,
)
from dexbot.execution import (
    CcxtBroker,
    DemoHostError,
    SwitchingMarketData,
    _resolved_host,
    assert_demo_urls,
    configure_bybit_demo,
    make_public_client,
    make_trading_client,
    wallet_totals,
)
from dexbot.llm import ProviderError, ResilientLLM, build_llm
from dexbot.market import compute_stats, make_candles
from dexbot.models import BaseAnalysis, NewsReport
from dexbot.rules import RuleBasedLLM
from dexbot.runtime import build_runtime


def test_demo_client_is_not_mainnet_or_testnet():
    client = make_trading_client("bybit", "k", "s", kind="demo", allow_mainnet=False)
    assert_demo_urls(client)
    assert client.options["enableDemoTrading"] is True
    assert not client.isSandboxModeEnabled
    host = _resolved_host(client.urls["api"]["private"], client.hostname)
    assert host == "api-demo.bybit.com"
    # The flag must not retarget an already-built demo client onto mainnet.
    still_demo = make_trading_client("bybit", "k", "s", kind="demo", allow_mainnet=True)
    assert_demo_urls(still_demo)


def test_url_override_pins_every_private_host():
    class Bare:
        urls = {
            "api": {
                "private": "https://api.bybit.com",
                "spot": "https://api.bybit.com",
                "public": "https://api.bybit.com",
            }
        }
        options: dict = {}
        hostname = "bybit.com"

    client = Bare()
    assert configure_bybit_demo(client) == "url_override"
    assert_demo_urls(client)
    assert client.options["enableUnifiedAccount"] is True
    assert client.urls["api"]["private"] == "https://api-demo.bybit.com"


def test_demo_broker_reads_the_unified_wallet():
    class Client:
        hostname = "bybit.com"
        urls = {
            "api": {
                "private": "https://api-demo.bybit.com",
                "spot": "https://api-demo.bybit.com",
            }
        }

        def __init__(self) -> None:
            self.params = None

        def fetch_balance(self, params=None):
            self.params = params
            return {
                "USDT": {"free": 4, "used": 0, "total": 4},
                "USDC": {"free": 1, "used": 0, "total": 1},
            }

    client = Client()
    broker = CcxtBroker(client, kind="demo", exchange_name="bybit", allow_mainnet=False)
    assert broker.get_cash() == pytest.approx(4)
    assert client.params == {"type": "unified"}


def test_broker_refuses_mainnet_host_before_any_order():
    class Bad:
        hostname = "bybit.com"
        urls = {"api": {"private": "https://api.bybit.com", "spot": "https://api.bybit.com"}}

        def create_market_buy_order(self, *args, **kwargs):
            raise AssertionError("order must not be sent")

    with pytest.raises(DemoHostError):
        CcxtBroker(Bad(), kind="demo", exchange_name="bybit", allow_mainnet=False)


def test_public_clients_have_no_key_and_demo_probe_is_isolated():
    public = make_public_client("bybit", demo=False)
    probe = make_public_client("bybit", demo=True)
    assert not public.apiKey
    assert not probe.apiKey
    assert _resolved_host(public.urls["api"]["public"], public.hostname) == "api.bybit.com"
    assert_demo_urls(probe)


def test_market_data_falls_back_when_demo_book_is_empty():
    class Empty:
        def fetch_tickers(self):
            return []

        def fetch_ohlcv(self, symbol):
            return []

    class Live:
        def __init__(self):
            self.calls = 0

        def fetch_tickers(self):
            self.calls += 1
            return ["btc"]

        def fetch_ohlcv(self, symbol):
            return ["candle"]

    live = Live()
    switched = SwitchingMarketData(Empty(), live)
    assert switched.fetch_tickers() == ["btc"]
    assert switched.using_secondary
    assert switched.fetch_ohlcv("BTC/USDT") == ["candle"]

    class Boom:
        def fetch_tickers(self):
            raise RuntimeError("demo public down")

        def fetch_ohlcv(self, symbol):
            raise RuntimeError("demo public down")

    switched = SwitchingMarketData(Boom(), live)
    assert switched.fetch_tickers() == ["btc"]
    assert live.calls == 2


def test_unified_wallet_totals_keep_the_demo_coins():
    totals = wallet_totals(
        {
            "USDT": {"free": 1000, "used": 1, "total": 1001},
            "USDC": {"free": 5, "used": 0, "total": 5},
            "BTC": {"free": 0.1, "used": 0, "total": 0.1},
            "ETH": {"free": 2, "used": 0, "total": 2},
            "DOGE": {"free": 0, "used": 0, "total": 0},
            "info": {"apiKey": "super-secret-key"},
            "total": {"USDT": 1001},
        }
    )
    assert totals["USDT"] == pytest.approx(1001)
    assert totals["USDC"] == pytest.approx(5)
    assert totals["BTC"] == pytest.approx(0.1)
    assert totals["ETH"] == pytest.approx(2)
    assert "DOGE" not in totals
    assert "info" not in totals


def test_withdraw_permission_is_detected_without_returning_the_key():
    clean = parse_key_permissions(
        {
            "retCode": 0,
            "result": {
                "apiKey": "super-secret-key",
                "readOnly": 0,
                "permissions": {"Spot": ["SpotTrade"], "Wallet": ["AccountTransfer"]},
            },
        }
    )
    assert clean is not None
    assert clean.read_only is False
    assert clean.withdraw is False
    dirty = parse_key_permissions(
        {
            "retCode": 0,
            "result": {
                "apiKey": "super-secret-key",
                "readOnly": 1,
                "permissions": {"Wallet": ["AccountTransfer", "Withdraw"]},
            },
        }
    )
    assert dirty is not None
    assert dirty.read_only is True
    assert dirty.withdraw is True
    assert not hasattr(dirty, "api_key")


class _FakeDemo:
    hostname = "bybit.com"
    urls = {
        "api": {
            "private": "https://api-demo.bybit.com",
            "spot": "https://api-demo.bybit.com",
            "public": "https://api-demo.bybit.com",
        }
    }

    def __init__(self, withdraw: bool) -> None:
        self.withdraw = withdraw
        self.params = None

    def privateGetV5UserQueryApi(self):
        wallet = ["AccountTransfer", "Withdraw"] if self.withdraw else ["AccountTransfer"]
        return {
            "retCode": 0,
            "result": {
                "apiKey": "super-secret-key",
                "readOnly": 0,
                "permissions": {"Spot": ["SpotTrade"], "Wallet": wallet},
            },
        }

    def fetch_balance(self, params=None):
        self.params = params
        return {
            "USDT": {"free": 10, "used": 0, "total": 10},
            "USDC": {"free": 1, "used": 0, "total": 1},
            "BTC": {"free": 0.01, "used": 0, "total": 0.01},
            "ETH": {"free": 0.2, "used": 0, "total": 0.2},
            "info": {"apiKey": "super-secret-key", "secret": "super-secret-secret"},
        }


def test_check_prints_balances_and_hides_secrets(monkeypatch):
    monkeypatch.setenv("BYBIT_DEMO_API_KEY", "super-secret-key")
    monkeypatch.setenv("BYBIT_DEMO_API_SECRET", "super-secret-secret")
    client = _FakeDemo(withdraw=False)
    settings = load_settings(package_config_path())
    report = inspect_connection(settings, client=client)
    text = render_check(report)
    assert report.exit_code == 0
    assert report.keys_set is True
    assert report.read_only is False
    assert report.withdraw_permission is False
    assert client.params == {"type": "unified"}
    assert "balance USDT=10" in text
    assert "balance BTC=0.01" in text
    assert "super-secret" not in text
    assert "keys_set=true" in text


def test_check_refuses_withdrawal_permission(monkeypatch):
    monkeypatch.setenv("BYBIT_DEMO_API_KEY", "super-secret-key")
    monkeypatch.setenv("BYBIT_DEMO_API_SECRET", "super-secret-secret")
    settings = load_settings(package_config_path())
    report = inspect_connection(settings, client=_FakeDemo(withdraw=True))
    text = render_check(report)
    assert report.exit_code == 2
    assert "refusing to run: withdrawal permission is enabled" in text
    assert "super-secret" not in text


def test_check_reports_missing_keys_without_calling_the_network(monkeypatch):
    monkeypatch.delenv("BYBIT_DEMO_API_KEY", raising=False)
    monkeypatch.delenv("BYBIT_DEMO_API_SECRET", raising=False)
    report = inspect_connection(load_settings(package_config_path()))
    assert report.keys_set is False
    assert report.exit_code == 1
    assert "BYBIT_DEMO_API_KEY" in report.detail
    assert "keys_set=false" in render_check(report)


def test_check_command_uses_the_report(monkeypatch, capsys):
    from dexbot.check import CheckReport

    monkeypatch.setattr(
        "dexbot.cli.inspect_connection",
        lambda settings, client=None: CheckReport(
            exchange="bybit",
            desk_mode="paper",
            exchange_mode="demo",
            keys_set=True,
            read_only=False,
            withdraw_permission=False,
            balances={"USDT": 3},
            detail="ok",
            exit_code=0,
        ),
    )
    code = main(["check", "--config", str(package_config_path())])
    out = capsys.readouterr().out
    assert code == 0
    assert "keys_set=true" in out
    assert "exchange_mode=demo" in out
    assert "balance USDT=3" in out


def test_mode_demo_matches_exchange_and_rejects_mismatches(monkeypatch):
    monkeypatch.setenv("DEXBOT_MODE", "demo")
    settings = load_settings(package_config_path())
    assert settings.mode == "demo"
    assert settings.exchange.mode == "demo"
    monkeypatch.setenv("DEXBOT_MODE", "testnet")
    with pytest.raises(ValidationError):
        load_settings(package_config_path())
    with pytest.raises(ValidationError):
        Settings(mode="testnet", exchange=ExchangeConfig(name="bybit", mode="demo"))
    with pytest.raises(ValidationError):
        Settings(mode="demo", exchange=ExchangeConfig(name="binance", mode="demo"))


def test_rules_buy_a_volume_spike_and_skip_a_quiet_tape():
    spiked = compute_stats(make_candles(price=100, spike_last=True, now_ms=10_000_000_000), VolumeConfig())
    quiet = compute_stats(make_candles(price=100, spike_last=False, now_ms=10_000_000_000), VolumeConfig())
    assert spiked.volume_flagged
    assert not quiet.volume_flagged
    base = BaseAnalysis(symbol="BTC/USDT", rating="hold", summary="seed", stance="wait")
    buy = run_second_layer(
        RuleBasedLLM(), Settings(), symbol="BTC/USDT", stats=spiked, base=base, mark=100
    )
    assert buy.decision.action == "buy"
    assert buy.news.overall == "none"
    assert "rule fallback" in buy.decision.reasoning
    skip = run_second_layer(
        RuleBasedLLM(), Settings(), symbol="ETH/USDT", stats=quiet, base=base, mark=100
    )
    assert skip.decision.action == "skip"
    assert skip.skeptic.vote == "skip"


def _xai_400():
    exc = Exception("Error code: 400")
    exc.status_code = 400
    exc.body = {"error": "Incorrect API key provided"}
    return exc


class _Models:
    def __init__(self, error: BaseException | None) -> None:
        self.error = error

    def list(self):
        if self.error is not None:
            raise self.error
        return []


class _Chat:
    def __init__(self, error: BaseException | None) -> None:
        self.error = error

    def create(self, **kwargs):
        if self.error is not None:
            raise self.error
        raise AssertionError("chat should not be called")


def _client(models_error, chat_error):
    return type("Client", (), {"models": _Models(models_error), "chat": type("Chat", (), {"completions": _Chat(chat_error)})()})()


def test_startup_probe_treats_http_400_incorrect_key_as_rejected(monkeypatch, caplog):
    monkeypatch.setenv("XAI_API_KEY", "sk-test-secret")
    caplog.set_level("WARNING")
    llm = build_llm(Settings(), client=_client(_xai_400(), None))
    assert llm.using_rules is True
    assert "LLM fallback" in caplog.text
    assert "HTTP 400" in caplog.text
    assert "sk-test-secret" not in caplog.text


def test_mid_run_400_auth_uses_rules_for_the_symbol(monkeypatch):
    monkeypatch.setenv("XAI_API_KEY", "sk-test-secret")
    llm = build_llm(Settings(), client=_client(None, _xai_400()))
    assert llm.using_rules is False
    quiet = compute_stats(make_candles(price=100, spike_last=False, now_ms=10_000_000_000), VolumeConfig())
    base = BaseAnalysis(symbol="BTC/USDT", rating="hold", summary="seed", stance="wait")
    layer = run_second_layer(llm, Settings(), symbol="BTC/USDT", stats=quiet, base=base, mark=100)
    assert llm.using_rules is True
    assert layer.decision.action == "skip"
    assert layer.notes == []
    assert "rule fallback" in layer.decision.reasoning


def test_http_400_about_response_format_is_not_an_auth_failure():
    class Format:
        last_citations: list[str] = []

        def complete_json(self, **kwargs):
            raise ProviderError(
                "Error code: 400 - response_format is not supported",
                status_code=400,
                body={"error": {"message": "response_format is not supported"}},
            )

    llm = ResilientLLM(Format(), RuleBasedLLM(), env_name="XAI_API_KEY")
    with pytest.raises(ProviderError):
        llm.complete_json(
            agent="news",
            symbol="BTC/USDT",
            schema=NewsReport,
            system="s",
            user="{}",
            search=False,
            model="grok",
        )
    assert llm.using_rules is False


def test_invalid_xai_key_switches_to_rules():
    class Rejected:
        last_citations: list[str] = []

        def complete_json(self, **kwargs):
            raise ProviderError("Error code: 401 invalid api key")

    llm = ResilientLLM(Rejected(), RuleBasedLLM(), env_name="XAI_API_KEY")
    parsed = llm.complete_json(
        agent="news",
        symbol="BTC/USDT",
        schema=NewsReport,
        system="s",
        user="{}",
        search=True,
        model="grok",
    )
    assert llm.using_rules is True
    assert parsed.overall == "none"
    again = llm.complete_json(
        agent="news",
        symbol="BTC/USDT",
        schema=NewsReport,
        system="s",
        user="{}",
        search=True,
        model="grok",
    )
    assert again.stance == "wait"

    class Down:
        def complete_json(self, **kwargs):
            raise ProviderError("timeout talking to provider")

    other = ResilientLLM(Down(), RuleBasedLLM(), env_name="XAI_API_KEY")
    with pytest.raises(ProviderError):
        other.complete_json(
            agent="news",
            symbol="BTC/USDT",
            schema=NewsReport,
            system="s",
            user="{}",
            search=False,
            model="grok",
        )
    assert other.using_rules is False


def test_missing_xai_key_logs_fallback(monkeypatch, caplog):
    monkeypatch.delenv("XAI_API_KEY", raising=False)
    caplog.set_level("WARNING")
    llm = build_llm(Settings())
    assert llm.using_rules is True
    assert "LLM fallback" in caplog.text
    assert "XAI_API_KEY" in caplog.text


def test_demo_runtime_keeps_orders_off_the_public_client(tmp_path, monkeypatch):
    monkeypatch.setenv("BYBIT_DEMO_API_KEY", "demo-key")
    monkeypatch.setenv("BYBIT_DEMO_API_SECRET", "demo-secret")
    monkeypatch.delenv("XAI_API_KEY", raising=False)
    settings = load_settings(package_config_path())
    settings.mode = "demo"
    settings.journal.path = str(tmp_path / "journal.sqlite")
    settings.risk.kill_switch_file = str(tmp_path / "KILL")
    runtime = build_runtime(settings)
    try:
        assert runtime.venue.name == "bybit-demo"
        assert runtime.llm.using_rules is True
        broker = runtime.venue.broker
        assert broker.client.apiKey == "demo-key"
        assert_demo_urls(broker.client)
        market = runtime.venue.market
        assert isinstance(market, SwitchingMarketData)
        assert not market.primary.client.apiKey
        assert not market.secondary.client.apiKey
        assert_demo_urls(market.primary.client)
        public_host = _resolved_host(
            market.secondary.client.urls["api"]["public"],
            market.secondary.client.hostname,
        )
        assert public_host == "api.bybit.com"
        assert market.primary.client is not broker.client
        assert "demo-secret" not in json.dumps(market.secondary.client.urls)
    finally:
        runtime.journal.close()


def test_demo_report_uses_exchange_balance(tmp_path):
    from datetime import datetime, timezone

    from dexbot.journal import Journal
    from dexbot.models import Fill
    from dexbot.report import load_report, render_report

    journal = Journal(tmp_path / "journal.sqlite")
    now = datetime.now(timezone.utc)
    run_id = journal.start_run(mode="demo", exchange="bybit-demo", mock=False, started_at=now)
    decision_id = journal.add_decision(
        run_id,
        symbol="BTC/USDT",
        action="buy",
        skeptic_vote="buy",
        executed=True,
        block_reason=None,
        payload={},
        created_at=now,
    )
    journal.record_entry(
        run_id=run_id,
        decision_id=decision_id,
        mode="demo",
        fill=Fill("o1", "BTC/USDT", "buy", 100, 2, 200, 0, "filled"),
        stop_loss=90,
        take_profit=130,
        created_at=now,
        cash=None,
    )
    journal.mark_positions({"BTC/USDT": 110})

    class Client:
        hostname = "bybit.com"
        urls = {"api": {"private": "https://api-demo.bybit.com", "spot": "https://api-demo.bybit.com"}}

        def __init__(self) -> None:
            self.params = None

        def fetch_balance(self, params=None):
            self.params = params
            return {"USDT": {"free": 2500, "used": 0, "total": 2500.5}}

    client = Client()
    try:
        paper = render_report(load_report(Settings(), journal))
        report = load_report(Settings(mode="demo"), journal, client=client)
        text = render_report(report)
    finally:
        journal.close()
    assert "Кэш: n/a" in paper
    assert client.params == {"type": "unified"}
    assert report["cash"] == pytest.approx(2500.5)
    assert report["equity"] == pytest.approx(2500.5 + 2 * 110)
    assert "Кэш: 2500.50 USDT" in text
    assert "Эквити (кэш + оценка позиций): 2720.50 USDT" in text


def test_mock_from_demo_config_stays_on_paper(tmp_path):
    settings = load_settings(package_config_path())
    settings.mode = "demo"
    settings.journal.path = str(tmp_path / "journal.sqlite")
    settings.risk.kill_switch_file = str(tmp_path / "KILL")
    runtime = build_runtime(settings, mock=True)
    try:
        assert runtime.settings.mode == "paper"
        assert runtime.venue.name == "paper"
    finally:
        runtime.journal.close()
