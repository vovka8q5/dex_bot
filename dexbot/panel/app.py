"""Local dashboard. Loopback by default. It never prints API keys or enables mainnet."""

from __future__ import annotations

import secrets
import subprocess
import sys
import time
from pathlib import Path

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from dexbot.config import Settings
from dexbot.journal import Journal
from dexbot.panel.state import build_state

_STATIC = Path(__file__).resolve().parent / "static"
_LOOPBACK = {"127.0.0.1", "localhost", "::1"}


def _journal(settings: Settings) -> Journal:
    return Journal(settings.journal.path)


def _check_token(request: Request) -> JSONResponse | None:
    expected = request.app.state.token
    sent = request.headers.get("x-dexbot-token", "")
    if not sent or not secrets.compare_digest(sent, expected):
        return JSONResponse({"ok": False, "detail": "Нужен токен этой страницы."}, status_code=403)
    return None


def _kill_path(settings: Settings) -> Path:
    return Path(settings.risk.kill_switch_file).expanduser()


def _exchange_balances(app) -> tuple[dict | None, str | None]:
    loader = app.state.exchange_loader
    if loader is None:
        return None, None
    now = time.monotonic()
    cached = app.state.exchange_cache
    if cached and now - cached["at"] < 60:
        return cached["balances"], cached["error"]
    try:
        balances = loader()
        error = None
    except Exception:
        balances = None
        error = "Баланс биржи сейчас недоступен. Показан журнал."
    app.state.exchange_cache = {"at": now, "balances": balances, "error": error}
    return balances, error


def _state(request: Request, *, refresh: bool = False) -> dict:
    if refresh:
        request.app.state.exchange_cache = None
    settings: Settings = request.app.state.settings
    balances, error = _exchange_balances(request.app)
    journal = _journal(settings)
    try:
        return build_state(settings, journal, exchange_balances=balances, exchange_error=error)
    finally:
        journal.close()


async def index(_: Request) -> HTMLResponse:
    html = (_STATIC / "index.html").read_text(encoding="utf-8")
    html = html.replace("{{TOKEN}}", _.app.state.token)
    return HTMLResponse(html)


async def api_state(request: Request) -> JSONResponse:
    return JSONResponse(_state(request))


async def api_refresh(request: Request) -> JSONResponse:
    denied = _check_token(request)
    if denied:
        return denied
    return JSONResponse(_state(request, refresh=True))


async def api_kill(request: Request) -> JSONResponse:
    denied = _check_token(request)
    if denied:
        return denied
    path = _kill_path(request.app.state.settings)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("1\n", encoding="utf-8")
    return JSONResponse({"ok": True, "detail": "Kill switch включён."})


async def api_resume(request: Request) -> JSONResponse:
    denied = _check_token(request)
    if denied:
        return denied
    path = _kill_path(request.app.state.settings)
    if path.is_file():
        path.unlink()
    return JSONResponse({"ok": True, "detail": "Kill switch снят. Новые входы снова возможны."})


async def api_run_once(request: Request) -> JSONResponse:
    denied = _check_token(request)
    if denied:
        return denied
    from dexbot.heartbeat import process_status

    status = process_status(request.app.state.settings)
    if status["running"]:
        return JSONResponse(
            {"ok": False, "detail": "Стол уже работает. Дождитесь его цикла."},
            status_code=409,
        )
    proc = request.app.state.run_once_proc
    if proc is not None and proc.poll() is None:
        return JSONResponse({"ok": False, "detail": "Один проход уже запущен."}, status_code=409)
    spawn = request.app.state.spawn
    request.app.state.run_once_proc = spawn()
    return JSONResponse({"ok": True, "detail": "Запущен один проход. Ордера возможны, если нет kill switch."})


def _default_spawn(settings: Settings, config_path: str | None):
    def spawn():
        command = [sys.executable, "-m", "dexbot", "run", "--once"]
        if config_path:
            command.extend(["--config", config_path])
        return subprocess.Popen(command)

    return spawn


def create_app(
    settings: Settings,
    *,
    config_path: str | None = None,
    exchange_loader=None,
    spawn=None,
) -> Starlette:
    app = Starlette(
        routes=[
            Route("/", index),
            Route("/api/state", api_state),
            Route("/api/refresh", api_refresh, methods=["POST"]),
            Route("/api/kill", api_kill, methods=["POST"]),
            Route("/api/resume", api_resume, methods=["POST"]),
            Route("/api/run-once", api_run_once, methods=["POST"]),
            Mount("/static", app=StaticFiles(directory=_STATIC), name="static"),
        ]
    )
    app.state.settings = settings
    app.state.token = secrets.token_urlsafe(24)
    app.state.config_path = config_path
    app.state.exchange_loader = exchange_loader
    app.state.exchange_cache = None
    app.state.spawn = spawn or _default_spawn(settings, config_path)
    app.state.run_once_proc = None
    return app


def _live_exchange_loader(settings: Settings):
    def load():
        if settings.mode == "paper":
            return None
        from dexbot.config import exchange_credentials
        from dexbot.execution import CcxtBroker, make_trading_client

        key, secret = exchange_credentials(settings)
        client = make_trading_client(
            settings.exchange.name,
            key,
            secret,
            kind=settings.mode,
            allow_mainnet=settings.allow_mainnet,
        )
        broker = CcxtBroker(
            client,
            kind=settings.mode,
            exchange_name=settings.exchange.name,
            allow_mainnet=settings.allow_mainnet,
        )
        return broker.asset_balances()

    return load


def serve_panel(settings: Settings, *, host: str, port: int, config_path: str | None, allow_lan: bool) -> None:
    import uvicorn

    if host not in _LOOPBACK and not allow_lan:
        raise SystemExit(
            "Панель слушает только 127.0.0.1. Для локальной сети передайте --allow-lan "
            "и не пробрасывайте порт в интернет: в панели нет пароля."
        )
    if host not in _LOOPBACK:
        print(
            "Внимание: панель доступна в локальной сети. Не публикуйте порт без отдельной защиты.",
            flush=True,
        )
    uvicorn.run(
        create_app(settings, config_path=config_path, exchange_loader=_live_exchange_loader(settings)),
        host=host,
        port=port,
        log_level="info",
    )
