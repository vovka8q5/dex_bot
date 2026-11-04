"""One hourly pass: filter, analyze, gate, then paper or testnet orders."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path

from dexbot.agents import run_second_layer
from dexbot.filter import filter_tokens
from dexbot.execution import PaperBroker
from dexbot.journal import Journal
from dexbot.logging_setup import event
from dexbot.market import TIMEFRAME_MS, compute_stats, drop_forming
from dexbot.models import CycleSummary, Fill
from dexbot.risk import RiskGuard, assess_market_plan, execution_gate
from dexbot.runtime import Runtime
from dexbot.sizing import size_order

logger = logging.getLogger("dexbot.pipeline")


def _paper_cash(runtime: Runtime) -> float | None:
    if isinstance(runtime.venue.broker, PaperBroker):
        return runtime.venue.broker.cash
    return None


def _sync_cash(runtime: Runtime) -> None:
    cash = _paper_cash(runtime)
    if cash is not None:
        runtime.journal.set_cash(cash)


def _equity(runtime: Runtime) -> float:
    broker = runtime.venue.broker
    if isinstance(broker, PaperBroker):
        cash = broker.cash
    else:
        cash = broker.get_cash()
    total = cash
    for pos in runtime.journal.open_positions():
        try:
            mark = broker.mark(pos.symbol)
        except Exception:
            mark = pos.last_mark or pos.avg_entry
        total += pos.qty_base * mark
    return total


def _refund(runtime: Runtime, fill: Fill) -> None:
    broker = runtime.venue.broker
    if isinstance(broker, PaperBroker) and fill.side == "buy":
        broker.cash += fill.qty_quote + fill.fee_quote
    elif isinstance(broker, PaperBroker) and fill.side == "sell":
        broker.cash -= fill.qty_quote - fill.fee_quote


def _close(runtime: Runtime, run_id: int, pos_id: int, symbol: str, qty: float, reason: str, now: datetime) -> bool:
    try:
        fill = runtime.venue.broker.sell(symbol, qty)
    except Exception:
        logger.exception("exit_failed symbol=%s reason=%s", symbol, reason)
        return False
    try:
        pnl = runtime.journal.record_exit(
            run_id=run_id,
            position_id=pos_id,
            mode=runtime.settings.mode,
            fill=fill,
            reason=reason,
            created_at=now,
            cash=_paper_cash(runtime),
        )
    except Exception:
        _refund(runtime, fill)
        logger.exception("exit_journal_failed symbol=%s", symbol)
        return False
    event(logger, "position_closed", symbol=symbol, reason=reason, pnl=round(pnl, 6), run_id=run_id)
    return True


def _kill_on(runtime: Runtime, kill: bool) -> bool:
    if kill or runtime.settings.risk.kill_switch:
        return True
    return Path(runtime.settings.risk.kill_switch_file).exists()


def run_cycle(runtime: Runtime, *, kill: bool = False, now: datetime | None = None) -> CycleSummary:
    moment = now or datetime.now(timezone.utc)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    run_id = runtime.journal.start_run(
        mode=runtime.settings.mode,
        exchange=runtime.venue.name,
        mock=runtime.mock,
        started_at=moment,
    )
    try:
        summary = _run(runtime, run_id, moment, kill)
    except Exception as exc:
        runtime.journal.finish_run(run_id, "error", note=str(exc), finished_at=moment)
        raise
    runtime.journal.finish_run(run_id, summary.status, finished_at=moment)
    event(
        logger,
        "cycle_complete",
        run_id=run_id,
        status=summary.status,
        accepted=summary.accepted,
        executed=summary.executed,
        blocked=summary.blocked,
        closed=summary.closed,
    )
    return summary


def _run(runtime: Runtime, run_id: int, now: datetime, kill: bool) -> CycleSummary:
    settings = runtime.settings
    journal = runtime.journal
    tickers = runtime.venue.market.fetch_tickers()
    runtime.venue.broker.set_marks({ticker.symbol: ticker.last for ticker in tickers})
    journal.mark_positions({ticker.symbol: ticker.last for ticker in tickers})

    if journal.day_start_equity(now) is None:
        journal.snapshot_equity(now, _equity(runtime))

    closed = 0
    if _kill_on(runtime, kill):
        for pos in journal.open_positions():
            if _close(runtime, run_id, pos.id, pos.symbol, pos.qty_base, "kill_switch", now):
                closed += 1
        journal.snapshot_equity(now, _equity(runtime))
        return CycleSummary(
            run_id=run_id,
            mode=settings.mode,
            accepted=0,
            rejected=0,
            executed=0,
            blocked=0,
            closed=closed,
            status="kill_switch",
        )

    for pos in list(journal.open_positions()):
        try:
            mark = runtime.venue.broker.mark(pos.symbol)
        except Exception:
            mark = pos.last_mark
        if mark <= pos.stop_loss:
            reason = "stop"
        elif mark >= pos.take_profit:
            reason = "take_profit"
        else:
            continue
        if _close(runtime, run_id, pos.id, pos.symbol, pos.qty_base, reason, now):
            closed += 1

    outcome = filter_tokens(tickers, settings.filter, now=now)
    for row in outcome.considered:
        journal.add_candidate(
            run_id,
            symbol=row.ticker.symbol,
            quote_volume=row.ticker.quote_volume_24h,
            spread_bps=row.spread_bps,
            last_price=row.ticker.last,
            passed=row.passed,
            reject_reason=row.reason,
            payload={"base": row.ticker.base, "listed_at": row.ticker.listed_at},
        )

    executed = 0
    blocked = 0
    guard = RiskGuard(settings.risk)
    tf_ms = TIMEFRAME_MS.get(settings.market.candle_timeframe, 3_600_000)
    now_ms = int(now.timestamp() * 1000)
    min_bars = settings.volume.lookback + 2

    for row in outcome.accepted:
        symbol = row.ticker.symbol
        try:
            did_execute = _symbol(
                runtime,
                run_id,
                row.ticker.symbol,
                row.ticker.last,
                now,
                now_ms,
                tf_ms,
                min_bars,
                guard,
            )
        except Exception:
            logger.exception("symbol_failed symbol=%s", symbol)
            journal.add_decision(
                run_id,
                symbol=symbol,
                action="skip",
                skeptic_vote=None,
                executed=False,
                block_reason="symbol_error",
                payload={},
                created_at=now,
            )
            blocked += 1
            continue
        if did_execute:
            executed += 1
        else:
            blocked += 1

    journal.snapshot_equity(now, _equity(runtime))
    return CycleSummary(
        run_id=run_id,
        mode=settings.mode,
        accepted=len(outcome.accepted),
        rejected=sum(1 for row in outcome.considered if not row.passed),
        executed=executed,
        blocked=blocked,
        closed=closed,
        status="ok",
    )


def _symbol(
    runtime: Runtime,
    run_id: int,
    symbol: str,
    mark: float,
    now: datetime,
    now_ms: int,
    tf_ms: int,
    min_bars: int,
    guard: RiskGuard,
) -> bool:
    settings = runtime.settings
    journal = runtime.journal
    raw = runtime.venue.market.fetch_ohlcv(symbol)
    candles = drop_forming(sorted(raw, key=lambda candle: candle.ts), now_ms=now_ms, timeframe_ms=tf_ms)
    if len(candles) < min_bars:
        journal.add_decision(
            run_id,
            symbol=symbol,
            action="skip",
            skeptic_vote=None,
            executed=False,
            block_reason="no_candles",
            payload={"bars": len(candles)},
            created_at=now,
        )
        return False
    stats = compute_stats(candles, settings.volume)
    base = runtime.analyzer.analyze(symbol, stats, candles)
    _log_agent(journal, run_id, symbol, "base_analysis", base.stance, base.summary, base.model_dump(), now)
    layer = run_second_layer(runtime.llm, settings, symbol=symbol, stats=stats, base=base, mark=mark)
    _log_agent(journal, run_id, symbol, "volume", layer.volume.stance, layer.volume.reasoning, layer.volume.model_dump(), now)
    _log_agent(journal, run_id, symbol, "news", layer.news.stance, layer.news.reasoning, layer.news.model_dump(), now)
    _log_agent(journal, run_id, symbol, "planner", layer.plan.stance, layer.plan.reasoning, layer.plan.model_dump(), now)
    _log_agent(journal, run_id, symbol, "risk_manager", layer.risk.stance, layer.risk.reasoning, layer.risk.model_dump(), now)
    _log_agent(journal, run_id, symbol, "skeptic", layer.skeptic.vote, layer.skeptic.reasoning, layer.skeptic.model_dump(), now)
    _log_agent(journal, run_id, symbol, "team_lead", layer.decision.action, layer.decision.reasoning, layer.decision.model_dump(), now)

    decision = layer.decision
    market_plan = assess_market_plan(
        mark=mark,
        entry=decision.entry,
        stop=decision.stop_loss,
        take_profit=decision.take_profit,
        min_reward_risk=settings.risk.min_reward_risk,
        max_deviation=settings.risk.max_entry_deviation_pct,
    )
    equity = _equity(runtime)
    day_start = journal.day_start_equity(now) or equity
    sized = None
    if market_plan.ok and decision.stop_loss is not None:
        sized = size_order(
            equity=equity,
            cash=_cash(runtime),
            entry=mark,
            stop=decision.stop_loss,
            fee_rate=settings.risk.fee_rate,
            max_risk_pct=settings.risk.max_equity_risk_pct,
        )
    sizing_ok = bool(sized and sized.ok)
    sizing_reason = sized.reason if sized else "not_sized"
    proposed = sized.risk_quote if sized and sized.ok else 0.0
    guard_result = guard.check(
        equity=equity,
        day_start_equity=day_start,
        open_positions=journal.open_count(),
        kill_switch=False,
        symbol_already_open=journal.has_open(symbol),
        proposed_risk=proposed if sizing_ok else 0.0,
    )
    gate = execution_gate(
        team_action=decision.action,
        skeptic_vote=layer.skeptic.vote,
        market_plan=market_plan,
        sizing_ok=sizing_ok,
        sizing_reason=sizing_reason,
        guard=guard_result,
    )
    payload = {
        "gate": gate.reason,
        "ratio": market_plan.ratio,
        "notes": layer.notes,
        "size": None if sized is None else sized.__dict__,
    }
    if not gate.allowed or sized is None or not sized.ok:
        journal.add_decision(
            run_id,
            symbol=symbol,
            action=decision.action,
            skeptic_vote=layer.skeptic.vote,
            executed=False,
            block_reason=gate.reason,
            payload=payload,
            created_at=now,
        )
        return False

    assert decision.stop_loss is not None and decision.take_profit is not None
    try:
        fill = runtime.venue.broker.buy(symbol, sized.qty_base)
    except Exception as exc:
        journal.add_decision(
            run_id,
            symbol=symbol,
            action=decision.action,
            skeptic_vote=layer.skeptic.vote,
            executed=False,
            block_reason=f"entry_error:{exc}",
            payload=payload,
            created_at=now,
        )
        return False
    decision_id = journal.add_decision(
        run_id,
        symbol=symbol,
        action="buy",
        skeptic_vote=layer.skeptic.vote,
        executed=True,
        block_reason=None,
        payload=payload,
        created_at=now,
    )
    try:
        journal.record_entry(
            run_id=run_id,
            decision_id=decision_id,
            mode=settings.mode,
            fill=fill,
            stop_loss=decision.stop_loss,
            take_profit=decision.take_profit,
            created_at=now,
            cash=_paper_cash(runtime),
        )
    except Exception:
        _refund(runtime, fill)
        raise
    event(
        logger,
        "order_filled",
        symbol=symbol,
        qty_base=round(fill.qty_base, 8),
        price=fill.price,
        risk_quote=round(sized.risk_quote, 4),
        run_id=run_id,
    )
    return True


def _cash(runtime: Runtime) -> float:
    if isinstance(runtime.venue.broker, PaperBroker):
        return runtime.venue.broker.cash
    return runtime.venue.broker.get_cash()


def _log_agent(journal: Journal, run_id: int, symbol: str, agent: str, stance: str, reasoning: str, payload: dict, now: datetime) -> None:
    journal.add_agent_output(
        run_id,
        symbol=symbol,
        agent=agent,
        stance=stance,
        reasoning=reasoning,
        payload=payload,
        created_at=now,
    )
