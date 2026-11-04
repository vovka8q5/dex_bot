"""Win rate, PnL, and a simple per-agent usefulness score."""

from __future__ import annotations

import argparse
from collections import defaultdict

from dexbot.config import load_settings
from dexbot.journal import Journal
from dexbot.models import Position

AGENTS = (
    "base_analysis",
    "volume",
    "news",
    "planner",
    "risk_manager",
    "skeptic",
    "team_lead",
)


def build_report(journal: Journal) -> dict:
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
    }


def _supported(closed: list[Position], stance: dict[tuple[int, str, str], str], agent: str) -> list[bool]:
    picked: list[bool] = []
    for pos in closed:
        if stance.get((pos.entry_run_id, pos.symbol, agent)) == "buy":
            picked.append(pos.realized_pnl > 0)
    return picked


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
        print(render_report(build_report(journal)))
    finally:
        journal.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
