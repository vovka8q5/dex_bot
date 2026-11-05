import os
from datetime import datetime, timezone

from starlette.testclient import TestClient

from dexbot.config import Settings
from dexbot.heartbeat import pid_alive, process_status, write_heartbeat
from dexbot.journal import Journal
from dexbot.models import Fill
from dexbot.panel.app import create_app, serve_panel
from dexbot.panel.state import orphan_balances


def _settings(tmp_path):
    settings = Settings()
    settings.journal.path = str(tmp_path / "journal.sqlite")
    settings.risk.kill_switch_file = str(tmp_path / "KILL")
    return settings


def _seed(journal: Journal) -> None:
    now = datetime(2026, 11, 5, 12, tzinfo=timezone.utc)
    run_id = journal.start_run(mode="demo", exchange="bybit-demo", mock=False, started_at=now)
    journal.add_agent_output(
        run_id,
        symbol="BTC/USDT",
        agent="skeptic",
        stance="buy",
        reasoning="объём подтверждён",
        payload={},
        created_at=now,
    )
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
        fill=Fill("b1", "BTC/USDT", "buy", 100, 1, 100, 0, "filled"),
        stop_loss=95,
        take_profit=110,
        created_at=now,
        cash=None,
    )
    journal.snapshot_equity(now, 1100)
    journal.record_llm_usage(
        run_id,
        [{
            "provider": "deepseek",
            "model": "deepseek-chat",
            "agent": "skeptic",
            "prompt_tokens": 10,
            "completion_tokens": 5,
            "total_tokens": 15,
            "estimated_cost_usd": 0.00001,
        }],
        created_at=now,
    )
    journal.finish_run(run_id, "ok", finished_at=now)


def test_orphan_balances_ignore_stables_and_journal_coins():
    rows = orphan_balances({"USDT": 1000, "ETH": 0.4, "LINK": 12, "BTC": 1}, ["BTC/USDT"])
    assert [row["asset"] for row in rows] == ["ETH", "LINK"]


def test_panel_state_and_controls(tmp_path, monkeypatch):
    monkeypatch.setenv("BYBIT_DEMO_API_SECRET", "super-secret-should-not-leak")
    settings = _settings(tmp_path)
    journal = Journal(settings.journal.path)
    _seed(journal)
    journal.close()
    spawned = []

    def spawn():
        spawned.append(True)

        class Proc:
            def poll(self):
                return None

        return Proc()

    app = create_app(
        settings,
        exchange_loader=lambda: {"USDT": 900.0, "ETH": 0.2, "LINK": 3.0, "BTC": 1.0},
        spawn=spawn,
    )
    client = TestClient(app)
    page = client.get("/")
    assert page.status_code == 200
    assert "локальная панель" in page.text
    assert app.state.token in page.text
    assert "super-secret" not in page.text

    state = client.get("/api/state").json()
    assert state["desk"]["mode"] == "paper"
    assert state["desk"]["kill_switch"] is False
    assert state["portfolio"]["cash"] == 900
    assert state["portfolio"]["positions"][0]["symbol"] == "BTC/USDT"
    assert [row["asset"] for row in state["portfolio"]["orphans"]] == ["ETH", "LINK"]
    assert state["charts"]["equity"][0]["equity"] == 1100
    assert state["charts"]["note"]
    assert state["agents"]["recent"][0]["agent"] == "skeptic"
    assert state["llm"]["latest"]["providers"] == ["deepseek"]
    assert "super-secret" not in client.get("/api/state").text

    denied = client.post("/api/kill")
    assert denied.status_code == 403
    assert not (tmp_path / "KILL").exists()

    headers = {"X-Dexbot-Token": app.state.token}
    killed = client.post("/api/kill", headers=headers)
    assert killed.status_code == 200
    assert (tmp_path / "KILL").is_file()
    assert client.get("/api/state").json()["desk"]["kill_switch"] is True
    assert client.post("/api/resume", headers=headers).status_code == 200
    assert not (tmp_path / "KILL").exists()

    write_heartbeat(settings, phase="sleeping", last_cycle_at="2026-11-05T12:00:00+00:00", next_cycle_at="2026-11-05T13:00:00+00:00")
    blocked = client.post("/api/run-once", headers=headers)
    assert blocked.status_code == 409
    assert spawned == []
    assert client.get("/api/state").json()["desk"]["running"] is True

    write_heartbeat(settings, phase="stopped")
    allowed = client.post("/api/run-once", headers=headers)
    assert allowed.status_code == 200
    assert spawned == [True]
    assert process_status(settings)["running"] is False


def test_panel_refuses_public_bind_without_a_flag(tmp_path):
    settings = _settings(tmp_path)
    try:
        serve_panel(settings, host="0.0.0.0", port=9, config_path=None, allow_lan=False)
    except SystemExit as exc:
        assert "127.0.0.1" in str(exc)
    else:
        raise AssertionError("public bind must be refused")


def test_pid_alive_sees_this_process_without_signaling_it():
    assert pid_alive(os.getpid())
    assert pid_alive(0) is False
