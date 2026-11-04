"""Win rate, PnL, and a simple per-agent usefulness score."""

from __future__ import annotations

import argparse
import logging
from collections import defaultdict

from dexbot.config import Settings, load_settings
from dexbot.journal import Journal
from dexbot.models import Position

logger = logging.getLogger("dexbot.report")

AGENTS = (
    "base_analysis",
    "volume",
    "news",
    "planner",
    "risk_manager",
    "skeptic",
    "team_lead",
)


def build_report(journal: Journal, *, cash: float | None = None) -> dict:
    """Usefulness is hit-rate on closed trades the agent marked ``buy``, minus the book win rate.

    Skipped names have no outcome, so this is not a counterfactual. An agent that
    said buy on every executed trade gets a usefulness of zero.
    """
    closed = journal.closed_positions()
    opened = journal.open_positions()
    wins = [pos for pos in closed if pos.realized_pnl > 0]
    win_rate = (len(wins) / len(closed)) if closed else None
    realized = sum(pos.realized_pnl for pos in closed)
    unrealized = sum((pos.last_mark - pos.avg_entry) * pos.qty_base - pos.buy_fee for pos in opened)
    if cash is None:
        cash = journal.get_cash()
    equity = None if cash is None else cash + sum(pos.qty_base * pos.last_mark for pos in opened)

    stance: dict[tuple[int, str, str], str] = {}
    votes: dict[str, dict[str, int]] = {agent: defaultdict(int) for agent in AGENTS}
    for row in journal.agent_rows():
        agent = row["agent"]
        if agent not in votes:
            votes[agent] = defaultdict(int)
        label = row["stance"] or "unknown"
        votes[agent][label] += 1
        stance[(int(row["run_id"]), row["symbol"], agent)] = label

    agents = {}
    for agent in AGENTS:
        supported = _supported(closed, stance, agent)
        hit = (sum(supported) / len(supported)) if supported else None
        usefulness = None if hit is None or win_rate is None else hit - win_rate
        agents[agent] = {
            "votes": dict(votes[agent]),
            "supported": len(supported),
            "hit_rate": hit,
            "usefulness": usefulness,
        }
    return {
        "closed_trades": len(closed),
        "wins": len(wins),
        "win_rate": win_rate,
        "realized_pnl": realized,
        "unrealized_pnl": unrealized,
        "cash": cash,
        "equity": equity,
        "open_positions": len(opened),
        "agents": agents,
        "llm": _llm_summary(journal),
    }


def _llm_summary(journal: Journal) -> dict:
    rows = journal.llm_usage_rows()
    latest_id = max((int(row["run_id"]) for row in rows), default=None)

    def pack(selected: list[dict]) -> dict:
        prompt = sum(int(row["prompt_tokens"] or 0) for row in selected)
        completion = sum(int(row["completion_tokens"] or 0) for row in selected)
        total = sum(int(row["total_tokens"] or 0) for row in selected)
        costs = [row["estimated_cost_usd"] for row in selected if row["estimated_cost_usd"] is not None]
        providers = sorted({row["provider"] for row in selected if row["provider"]})
        return {
            "calls": len(selected),
            "prompt_tokens": prompt,
            "completion_tokens": completion,
            "total_tokens": total,
            "estimated_cost_usd": None if not costs else float(sum(costs)),
            "providers": providers,
        }

    latest = [row for row in rows if latest_id is not None and int(row["run_id"]) == latest_id]
    return {"latest_run_id": latest_id, "latest": pack(latest), "all": pack(rows)}


def _supported(closed: list[Position], stance: dict[tuple[int, str, str], str], agent: str) -> list[bool]:
    picked: list[bool] = []
    for pos in closed:
        if stance.get((pos.entry_run_id, pos.symbol, agent)) == "buy":
            picked.append(pos.realized_pnl > 0)
    return picked


def fetch_exchange_usdt(settings: Settings, client=None) -> float:
    """USDT total on the private venue. Demo uses the unified wallet."""
    from dexbot.config import exchange_credentials
    from dexbot.execution import guard_client, make_trading_client, quote_total

    kind = settings.mode
    if client is None:
        key, secret = exchange_credentials(settings)
        client = make_trading_client(
            settings.exchange.name,
            key,
            secret,
            kind=kind,
            allow_mainnet=settings.allow_mainnet,
        )
    guard_client(
        client,
        kind=kind,
        name=settings.exchange.name,
        allow_mainnet=settings.allow_mainnet,
    )
    params = {"type": "unified"} if kind == "demo" and settings.exchange.name == "bybit" else {}
    balance = client.fetch_balance(params)
    return quote_total(balance, "USDT")


def load_report(settings: Settings, journal: Journal, client=None) -> dict:
    """Paper cash comes from the journal. Demo and testnet cash come from the exchange."""
    if settings.mode in {"demo", "testnet"} or (settings.mode == "mainnet" and settings.allow_mainnet):
        try:
            return build_report(journal, cash=fetch_exchange_usdt(settings, client=client))
        except Exception:
            logger.warning("exchange balance unavailable; report cash falls back to the journal")
    return build_report(journal)


def render_report(report: dict) -> str:
    win = "n/a" if report["win_rate"] is None else f"{report['win_rate'] * 100:.2f}%"
    lines = [
        f"Закрытых сделок: {report['closed_trades']}",
        f"Доля прибыльных: {win}",
        f"Реализованный PnL: {report['realized_pnl']:.2f} USDT",
        f"Нереализованный PnL: {report['unrealized_pnl']:.2f} USDT",
        f"Открытых позиций: {report['open_positions']}",
        f"Кэш: {_num(report['cash'])} USDT",
        f"Эквити (кэш + оценка позиций): {_num(report['equity'])} USDT",
        _llm_line("Последний прогон", report["llm"]["latest"], report["llm"]["latest_run_id"]),
        _llm_line("Все прогоны", report["llm"]["all"], None),
        "",
        "Полезность агента = доля прибыльных среди сделок, где stance был buy, минус общая доля прибыльных.",
        "Пропущенные идеи в расчёт не входят: по ним нет результата.",
        "",
        f"{'агент':<16} {'n':>4} {'hit':>8} {'usefulness':>12}  голоса",
    ]
    for name, stat in report["agents"].items():
        hit = "n/a" if stat["hit_rate"] is None else f"{stat['hit_rate'] * 100:.2f}%"
        useful = "n/a" if stat["usefulness"] is None else f"{stat['usefulness']:+.2f}"
        votes = ", ".join(f"{key}={value}" for key, value in sorted(stat["votes"].items())) or "-"
        lines.append(f"{name:<16} {stat['supported']:>4} {hit:>8} {useful:>12}  {votes}")
    return "\n".join(lines)


def _llm_line(label: str, block: dict, run_id: int | None) -> str:
    if not block["calls"]:
        suffix = f" #{run_id}" if run_id is not None else ""
        return f"LLM {label}{suffix}: нет вызовов"
    who = ",".join(block["providers"]) or "unknown"
    run = f" #{run_id}" if run_id is not None else ""
    cost = _usd(block["estimated_cost_usd"])
    return (
        f"LLM {label}{run} ({who}): токены {block['total_tokens']} "
        f"(prompt {block['prompt_tokens']}, completion {block['completion_tokens']}), оценка {cost}"
    )


def _usd(value: float | None) -> str:
    if value is None:
        return "n/a"
    if abs(value) < 0.01:
        return f"${value:.6f}"
    return f"${value:.4f}"


def _num(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.2f}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="dexbot.report", description="Отчёт по журналу сделок")
    parser.add_argument("--config", default=None)
    parser.add_argument("--journal", default=None)
    args = parser.parse_args(argv)
    settings = load_settings(args.config)
    if args.journal:
        settings.journal.path = args.journal
    journal = Journal(settings.journal.path)
    try:
        print(render_report(load_report(settings, journal)))
    finally:
        journal.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
