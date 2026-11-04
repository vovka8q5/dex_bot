"""Command line. ``run --once --mock`` is the offline dry-run."""

from __future__ import annotations

import argparse
import logging
import time

from dexbot.config import load_settings
from dexbot.logging_setup import configure_logging, event
from dexbot.pipeline import run_cycle
from dexbot.report import build_report, render_report
from dexbot.runtime import build_runtime
from dexbot.schedule import sleep_seconds

logger = logging.getLogger("dexbot.cli")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="dexbot",
        description="Paper/testnet multi-agent crypto desk. Mainnet stays off without allow_mainnet.",
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

    args = parser.parse_args(argv)
    settings = load_settings(args.config)
    if args.journal:
        settings.journal.path = args.journal
    configure_logging(settings.log_level, settings.log_json)

    if args.command == "report":
        from dexbot.journal import Journal

        journal = Journal(settings.journal.path)
        try:
            print(render_report(build_report(journal)))
        finally:
            journal.close()
        return 0

    if args.kill_switch:
        settings.risk.kill_switch = True
    return _serve(settings, once=args.once, mock=args.mock)


def _serve(settings, *, once: bool, mock: bool) -> int:
    runtime = build_runtime(settings, mock=mock)
    event(
        logger,
        "desk_start",
        mode=runtime.settings.mode,
        exchange=runtime.venue.name,
        mock=runtime.mock,
        allow_mainnet=runtime.settings.allow_mainnet,
    )
    try:
        while True:
            try:
                summary = run_cycle(runtime)
            except KeyboardInterrupt:
                return 0
            except Exception:
                logger.exception("cycle_failed")
                if once:
                    return 1
            else:
                print(summary.line(), flush=True)
                if once:
                    return 0
            delay = sleep_seconds(
                time.time(),
                runtime.settings.scheduler.interval_seconds,
                runtime.settings.scheduler.align_to_hour,
            )
            event(logger, "sleep", seconds=round(delay, 1))
            time.sleep(delay)
    except KeyboardInterrupt:
        return 0
    finally:
        runtime.journal.close()
