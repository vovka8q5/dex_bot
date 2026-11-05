"""Dashboard snapshot built from the journal the desk already writes."""

from __future__ import annotations

from dexbot.config import Settings
from dexbot.heartbeat import process_status
from dexbot.journal import Journal
from dexbot.report import AGENTS, build_report

_QUOTE_LIKE = {"USDT", "USDC", "USD", "EUR", "BUSD", "FDUSD", "DAI", "USDE", "TUSD", "PYUSD"}


def orphan_balances(balances: dict[str, float] | None, open_symbols: list[str]) -> list[dict]:
    """Coins on the exchange that the journal is not managing.

    Quote-like balances are cash, not positions. Importing the rest needs an
    entry and a stop, which this panel does not invent.
    """
    if not balances:
        return []
    held = {symbol.split("/")[0].upper() for symbol in open_symbols}
    orphans = []
    for asset, qty in sorted(balances.items()):
        name = str(asset).upper()
        amount = float(qty or 0)
        if name in _QUOTE_LIKE or amount <= 0 or name in held:
            continue
        orphans.append({"asset": name, "qty": amount})
    return orphans


def _drawdown(equities: list[float]) -> list[float]:
    peak = None
    points: list[float] = []
    for value in equities:
        peak = value if peak is None else max(peak, value)
        if not peak:
            points.append(0.0)
        else:
            points.append((value - peak) / peak)
    return points


def _positions(journal: Journal) -> list[dict]:
    rows = []
    for pos in journal.open_positions():
        unrealized = (pos.last_mark - pos.avg_entry) * pos.qty_base - pos.buy_fee
        rows.append(
            {
                "symbol": pos.symbol,
                "qty": pos.qty_base,
                "avg_entry": pos.avg_entry,
                "last_mark": pos.last_mark,
                "stop_loss": pos.stop_loss,
                "take_profit": pos.take_profit,
                "unrealized_pnl": unrealized,
                "opened_at": pos.opened_at,
            }
        )
    return rows


def _realized_series(journal: Journal) -> list[dict]:
    closed = sorted(journal.closed_positions(), key=lambda pos: pos.closed_at or pos.opened_at)
    cumulative = 0.0
    points = []
    for pos in closed:
        cumulative += pos.realized_pnl
        points.append(
            {
                "ts": pos.closed_at or pos.opened_at,
                "pnl": pos.realized_pnl,
                "cumulative": cumulative,
            }
        )
    return points


def _recent_votes(journal: Journal, limit: int = 24) -> list[dict]:
    rows = journal.agent_rows()
    if not rows:
        return []
    latest_run = max(int(row["run_id"]) for row in rows)
    chosen = [row for row in rows if int(row["run_id"]) == latest_run]
    chosen = chosen[-limit:]
    return [
        {
            "run_id": int(row["run_id"]),
            "symbol": row["symbol"],
            "agent": row["agent"],
            "stance": row["stance"],
            "reasoning": (row["reasoning"] or "")[:180],
            "created_at": row["created_at"],
        }
        for row in chosen
    ]


def build_state(
    settings: Settings,
    journal: Journal,
    *,
    exchange_balances: dict[str, float] | None = None,
    exchange_error: str | None = None,
) -> dict:
    usdt = None
    if exchange_balances and "USDT" in exchange_balances:
        usdt = float(exchange_balances["USDT"])
    report = build_report(journal, cash=usdt)
    positions = _positions(journal)
    series = journal.equity_series()
    equities = [point["equity"] for point in series]
    drawdown = _drawdown(equities)
    max_dd = min(drawdown) if drawdown else None
    runs = journal.recent_runs(1)
    last_run = runs[0] if runs else None
    proc = process_status(settings)
    kill_path = settings.risk.kill_switch_file
    from pathlib import Path

    kill_on = Path(kill_path).expanduser().is_file() or settings.risk.kill_switch
    note = None
    if len(series) < 2:
        note = "Мало точек эквити: снимок пишется в конце прохода. График заполнится после нескольких циклов."
    return {
        "desk": {
            "mode": settings.mode,
            "exchange": settings.exchange.name,
            "exchange_mode": settings.exchange.mode,
            "running": proc["running"],
            "phase": proc["phase"],
            "kill_switch": kill_on,
            "kill_switch_file": str(Path(kill_path)),
            "last_cycle_at": proc["last_cycle_at"] or (last_run["finished_at"] if last_run else None),
            "next_cycle_at": proc["next_cycle_at"],
            "interval_seconds": settings.scheduler.interval_seconds,
            "last_run": last_run,
        },
        "portfolio": {
            "cash": report["cash"],
            "equity": report["equity"],
            "realized_pnl": report["realized_pnl"],
            "unrealized_pnl": report["unrealized_pnl"],
            "open_count": report["open_positions"],
            "positions": positions,
            "orphans": orphan_balances(exchange_balances, [row["symbol"] for row in positions]),
            "exchange_error": exchange_error,
            "exchange_checked": exchange_balances is not None,
        },
        "charts": {
            "equity": series,
            "drawdown": [{"ts": point["ts"], "dd": dd} for point, dd in zip(series, drawdown)],
            "max_drawdown": max_dd,
            "realized": _realized_series(journal),
            "note": note,
        },
        "agents": {
            "names": list(AGENTS),
            "usefulness": report["agents"],
            "recent": _recent_votes(journal),
        },
        "llm": report["llm"],
    }
