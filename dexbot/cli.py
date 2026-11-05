"""Command line. ``run --once --mock`` is the offline dry-run."""

from __future__ import annotations

import argparse
import logging
import time
from datetime import datetime, timedelta, timezone

from dexbot.check import inspect_connection, render_check
from dexbot.config import load_settings
from dexbot.heartbeat import write_heartbeat
from dexbot.logging_setup import configure_logging, event
from dexbot.pipeline import run_cycle
from dexbot.report import load_report, render_report
from dexbot.runtime import build_runtime
from dexbot.schedule import sleep_seconds

logger = logging.getLogger("dexbot.cli")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="dexbot",
        description="Paper, Bybit demo, and testnet desk. Mainnet stays off without allow_mainnet.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="Run one pass or loop once per hour")
    run.add_argument("--once", action="store_true", help="Run a single pass and exit")
    run.add_argument("--mock", action="store_true", help="Paper fills, fixture book, scripted LLM, no network")
    run.add_argument("--config", default=None)
    run.add_argument("--journal", default=None)
    run.add_argument("--kill-switch", action="store_true", help="Close every open position and do not enter")

    report = sub.add_parser("report", help="Win rate, PnL, and per-agent usefulness")
    report.add_argument("--config", default=None)
    report.add_argument("--journal", default=None)

    check = sub.add_parser("check", help="Show whether keys are set, permissions, and balances")
    check.add_argument("--config", default=None)

    panel = sub.add_parser("panel", help="Local dashboard on 127.0.0.1")
    panel.add_argument("--host", default="127.0.0.1")
    panel.add_argument("--port", type=int, default=8787)
    panel.add_argument("--allow-lan", action="store_true", help="Bind beyond loopback. Do not publish the port.")
    panel.add_argument("--config", default=None)
    panel.add_argument("--journal", default=None)

    args = parser.parse_args(argv)
    settings = load_settings(args.config)
    if getattr(args, "journal", None):
        settings.journal.path = args.journal
    configure_logging(settings.log_level, settings.log_json)

    if args.command == "panel":
        from dexbot.panel.app import serve_panel

        serve_panel(
            settings,
            host=args.host,
            port=args.port,
            config_path=str(args.config) if args.config else None,
            allow_lan=args.allow_lan,
        )
        return 0

    if args.command == "check":
        report = inspect_connection(settings)
        print(render_check(report), flush=True)
        return report.exit_code

    if args.command == "report":
        from dexbot.journal import Journal

        journal = Journal(settings.journal.path)
        try:
            print(render_report(load_report(settings, journal)))
        finally:
            journal.close()
        return 0

    if args.kill_switch:
        settings.risk.kill_switch = True
    if not args.mock and settings.mode != "paper":
        preflight = inspect_connection(settings)
        print(render_check(preflight), flush=True)
        if preflight.exit_code != 0:
            return preflight.exit_code
    return _serve(settings, once=args.once, mock=args.mock)


def _serve(settings, *, once: bool, mock: bool) -> int:
    runtime = build_runtime(settings, mock=mock)
    last_cycle = None
    event(
        logger,
        "desk_start",
        mode=runtime.settings.mode,
        exchange=runtime.venue.name,
        mock=runtime.mock,
        allow_mainnet=runtime.settings.allow_mainnet,
        llm_fallback=bool(getattr(runtime.llm, "using_rules", False)),
        llm_provider=getattr(runtime.llm, "provider_name", "unknown"),
    )
    try:
        while True:
            write_heartbeat(settings, phase="running", last_cycle_at=last_cycle)
            try:
                summary = run_cycle(runtime)
            except KeyboardInterrupt:
                return 0
            except Exception:
                logger.exception("cycle_failed")
                if once:
                    return 1
            else:
                last_cycle = datetime.now(timezone.utc).isoformat(timespec="seconds")
                print(summary.line(), flush=True)
                if once:
                    return 0
            delay = sleep_seconds(
                time.time(),
                runtime.settings.scheduler.interval_seconds,
                runtime.settings.scheduler.align_to_hour,
            )
            next_at = (datetime.now(timezone.utc) + timedelta(seconds=delay)).isoformat(timespec="seconds")
            write_heartbeat(settings, phase="sleeping", last_cycle_at=last_cycle, next_cycle_at=next_at)
            event(logger, "sleep", seconds=round(delay, 1))
            time.sleep(delay)
    except KeyboardInterrupt:
        return 0
    finally:
        write_heartbeat(settings, phase="stopped", last_cycle_at=last_cycle)
        runtime.journal.close()
