"""Small status file the panel reads. The trading loop writes it; the panel never trades."""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

from dexbot.config import Settings


def heartbeat_path(settings: Settings) -> Path:
    return Path(settings.journal.path).expanduser().resolve().parent / "heartbeat.json"


def write_heartbeat(
    settings: Settings,
    *,
    phase: str,
    last_cycle_at: str | None = None,
    next_cycle_at: str | None = None,
    run_id: int | None = None,
) -> None:
    path = heartbeat_path(settings)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "pid": os.getpid(),
        "mode": settings.mode,
        "phase": phase,
        "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "last_cycle_at": last_cycle_at,
        "next_cycle_at": next_cycle_at,
        "run_id": run_id,
    }
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(payload), encoding="utf-8")
    temporary.replace(path)


def read_heartbeat(settings: Settings) -> dict | None:
    path = heartbeat_path(settings)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def pid_alive(pid: int) -> bool:
    """True when a process exists. Does not signal it.

    On Windows, ``os.kill(pid, 0)`` is not a harmless probe, so this uses
    ``OpenProcess`` there and ``os.kill(pid, 0)`` only elsewhere.
    """
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes

        access = 0x1000  # PROCESS_QUERY_LIMITED_INFORMATION
        handle = ctypes.windll.kernel32.OpenProcess(access, False, int(pid))
        if not handle:
            return False
        ctypes.windll.kernel32.CloseHandle(handle)
        return True
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    else:
        return True


def process_status(settings: Settings) -> dict:
    beat = read_heartbeat(settings)
    if not beat:
        return {"running": False, "phase": None, "last_cycle_at": None, "next_cycle_at": None, "run_id": None}
    pid = int(beat.get("pid") or 0)
    phase = str(beat.get("phase") or "")
    alive = pid_alive(pid)
    running = alive and phase != "stopped"
    return {
        "running": running,
        "phase": phase or None,
        "last_cycle_at": beat.get("last_cycle_at"),
        "next_cycle_at": beat.get("next_cycle_at"),
        "run_id": beat.get("run_id"),
        "updated_at": beat.get("updated_at"),
    }
