import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

from dexbot.config import load_settings, package_config_path
from dexbot.models import Fill
from dexbot.pipeline import run_cycle
from dexbot.runtime import build_runtime

ROOT = Path(__file__).resolve().parents[1]


def _runtime(tmp_path: Path):
    settings = load_settings(package_config_path())
    settings.journal.path = str(tmp_path / "journal.sqlite")
    settings.risk.kill_switch_file = str(tmp_path / "KILL")
    return build_runtime(settings, mock=True)


def test_mock_cycle_buys_btc_and_blocks_the_others(tmp_path):
    runtime = _runtime(tmp_path)
    summary = run_cycle(runtime)
    assert summary.status == "ok"
    assert summary.accepted == 3
    assert summary.executed == 1
    assert summary.blocked == 2
    assert runtime.settings.mode == "paper"
    assert runtime.venue.name == "paper"

    decisions = {row["symbol"]: row for row in runtime.journal.decisions()}
    assert decisions["BTC/USDT"]["executed"] == 1
    assert decisions["ETH/USDT"]["block_reason"] == "skeptic:skip"
    assert decisions["ETH/USDT"]["executed"] == 0
    assert decisions["SOL/USDT"]["block_reason"] == "reward_risk"

    orders = runtime.journal.orders()
    assert len(orders) == 1
    assert orders[0]["symbol"] == "BTC/USDT"
    assert orders[0]["side"] == "buy"
    assert orders[0]["qty_base"] == pytest.approx(20)
    assert orders[0]["mode"] == "paper"
    assert runtime.journal.get_cash() == pytest.approx(7998)
    assert runtime.journal.open_count() == 1

    reasons = {
        row["symbol"]: row["reject_reason"]
        for row in runtime.journal.conn.execute("SELECT symbol, reject_reason FROM candidates")
    }
    assert reasons["DOGE/USDT"] == "blocklist"
    assert reasons["ETH3L/USDT"] == "leveraged"
    assert reasons["NEW/USDT"] == "new_listing"
    assert reasons["WIDE/USDT"] == "spread"
    assert reasons["TINY/USDT"] == "min_volume"
    agents = {(row["symbol"], row["agent"]) for row in runtime.journal.agent_rows()}
    assert ("DOGE/USDT", "team_lead") not in agents
    assert len(runtime.journal.agent_rows()) == 21

    btc_volume = next(
        row for row in runtime.journal.agent_rows() if row["symbol"] == "BTC/USDT" and row["agent"] == "volume"
    )
    eth_volume = next(
        row for row in runtime.journal.agent_rows() if row["symbol"] == "ETH/USDT" and row["agent"] == "volume"
    )
    assert json.loads(btc_volume["payload_json"])["flagged"] is True
    assert json.loads(eth_volume["payload_json"])["flagged"] is False
    btc_news = next(
        row for row in runtime.journal.agent_rows() if row["symbol"] == "BTC/USDT" and row["agent"] == "news"
    )
    news = json.loads(btc_news["payload_json"])
    assert news["items"][0]["label"] == "serious news"
    assert news["citations"] == ["https://example.com/x"]
    runtime.journal.close()


def test_stop_and_kill_switch_close_without_a_new_buy(tmp_path):
    runtime = _runtime(tmp_path)
    assert run_cycle(runtime).executed == 1
    btc = next(ticker for ticker in runtime.venue.market.tickers if ticker.symbol == "BTC/USDT")
    btc.last = 90
    btc.bid = 89.95
    btc.ask = 90.05
    stopped = run_cycle(runtime)
    assert stopped.closed == 1
    assert stopped.executed == 0
    assert runtime.journal.open_count() == 0

    fresh = _runtime(tmp_path / "kill")
    assert run_cycle(fresh).executed == 1
    killed = run_cycle(fresh, kill=True)
    assert killed.status == "kill_switch"
    assert killed.closed == 1
    assert killed.executed == 0
    assert fresh.journal.open_count() == 0
    assert fresh.journal.get_cash() == pytest.approx(9996)
    exits = [row for row in fresh.journal.orders() if row["side"] == "sell"]
    assert exits[0]["reason"] == "kill_switch"
    runtime.journal.close()
    fresh.journal.close()


def test_mock_forces_paper_even_if_config_says_testnet(tmp_path, monkeypatch):
    monkeypatch.delenv("BYBIT_TESTNET_API_KEY", raising=False)
    monkeypatch.delenv("BYBIT_TESTNET_API_SECRET", raising=False)
    settings = load_settings(package_config_path())
    settings.mode = "testnet"
    settings.journal.path = str(tmp_path / "journal.sqlite")
    settings.risk.kill_switch_file = str(tmp_path / "KILL")
    runtime = build_runtime(settings, mock=True)
    assert runtime.settings.mode == "paper"
    assert runtime.mock is True
    summary = run_cycle(runtime)
    assert summary.executed == 1
    runtime.journal.close()


def test_module_dry_run(tmp_path):
    journal = tmp_path / "journal.sqlite"
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT)
    env.pop("DEXBOT_MODE", None)
    env.pop("DEXBOT_ALLOW_MAINNET", None)
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "dexbot",
            "run",
            "--once",
            "--mock",
            "--config",
            str(package_config_path()),
            "--journal",
            str(journal),
        ],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    assert "executed=1" in proc.stdout
    assert "mode=paper" in proc.stdout
    assert journal.exists()


def test_report_module_runs(tmp_path):
    runtime = _runtime(tmp_path)
    run_cycle(runtime)
    runtime.journal.close()
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT)
    proc = subprocess.run(
        [sys.executable, "-m", "dexbot.report", "--journal", str(tmp_path / "journal.sqlite"), "--config", str(package_config_path())],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    assert "Закрытых сделок" in proc.stdout


def test_manual_fills_match_report_math(tmp_path):
    """Guard the usefulness definition with two closed trades and zero fees."""
    from dexbot.journal import Journal
    from dexbot.report import build_report

    journal = Journal(tmp_path / "journal.sqlite")
    moment = datetime.now(timezone.utc)
    run_id = journal.start_run(mode="paper", exchange="paper", mock=True, started_at=moment)

    def _trade(symbol: str, exit_price: float, stance_volume: str, stance_skeptic: str) -> None:
        journal.add_agent_output(
            run_id, symbol=symbol, agent="volume", stance=stance_volume, reasoning="t", payload={}, created_at=moment
        )
        journal.add_agent_output(
            run_id, symbol=symbol, agent="skeptic", stance=stance_skeptic, reasoning="t", payload={}, created_at=moment
        )
        decision_id = journal.add_decision(
            run_id, symbol=symbol, action="buy", skeptic_vote=stance_skeptic, executed=True,
            block_reason=None, payload={}, created_at=moment,
        )
        buy = Fill(f"b-{symbol}", symbol, "buy", 100, 1, 100, 0, "filled")
        journal.record_entry(
            run_id=run_id, decision_id=decision_id, mode="paper", fill=buy,
            stop_loss=90, take_profit=120, created_at=moment, cash=None,
        )
        pos = journal.open_positions()[-1]
        sell = Fill(f"s-{symbol}", symbol, "sell", exit_price, 1, exit_price, 0, "filled")
        journal.record_exit(
            run_id=run_id, position_id=pos.id, mode="paper", fill=sell,
            reason="test", created_at=moment, cash=None,
        )

    _trade("AAA/USDT", 110, "buy", "wait")
    _trade("BBB/USDT", 90, "wait", "buy")
    report = build_report(journal)
    assert report["closed_trades"] == 2
    assert report["win_rate"] == pytest.approx(0.5)
    assert report["realized_pnl"] == pytest.approx(0)
    assert report["agents"]["volume"]["usefulness"] == pytest.approx(0.5)
    assert report["agents"]["skeptic"]["usefulness"] == pytest.approx(-0.5)
    journal.close()
