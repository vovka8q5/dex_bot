"""Connectivity check. Prints booleans and balances, never API secrets."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from dexbot.config import Settings, credential_env_names, keys_are_set, private_kind
from dexbot.execution import (
    DemoHostError,
    MainnetDisabled,
    guard_client,
    make_trading_client,
    wallet_totals,
)

logger = logging.getLogger("dexbot.check")


@dataclass
class KeyPermissions:
    read_only: bool
    withdraw: bool


@dataclass
class CheckReport:
    exchange: str
    desk_mode: str
    exchange_mode: str
    keys_set: bool
    read_only: bool | None = None
    withdraw_permission: bool | None = None
    balances: dict[str, float] = field(default_factory=dict)
    detail: str = ""
    exit_code: int = 0


def parse_key_permissions(payload: dict) -> KeyPermissions | None:
    """Read readOnly and withdraw from a Bybit query-api body.

    The raw body contains the API key. This function does not return it.
    """
    if not isinstance(payload, dict):
        return None
    if str(payload.get("retCode", 0)) != "0":
        return None
    result = payload.get("result") or {}
    if not isinstance(result, dict):
        return None
    read_only = int(result.get("readOnly") or 0) == 1
    return KeyPermissions(read_only=read_only, withdraw=_has_withdraw(result.get("permissions")))


def _has_withdraw(permissions: object) -> bool:
    if isinstance(permissions, dict):
        return any(_has_withdraw(value) for value in permissions.values())
    if isinstance(permissions, list):
        return any(_has_withdraw(item) for item in permissions)
    if isinstance(permissions, str):
        return "withdraw" in permissions.lower()
    return False


def render_check(report: CheckReport) -> str:
    lines = [
        f"desk_mode={report.desk_mode}",
        f"exchange={report.exchange}",
        f"exchange_mode={report.exchange_mode}",
        f"keys_set={str(report.keys_set).lower()}",
    ]
    if report.read_only is not None:
        lines.append(f"read_only={str(report.read_only).lower()}")
    else:
        lines.append("read_only=unknown")
    if report.withdraw_permission is not None:
        lines.append(f"withdraw_permission={str(report.withdraw_permission).lower()}")
    else:
        lines.append("withdraw_permission=unknown")
    if report.balances:
        for code, total in report.balances.items():
            lines.append(f"balance {code}={total:.8g}")
    else:
        lines.append("balances=none")
    if report.detail:
        lines.append(report.detail)
    return "\n".join(lines)


def inspect_connection(settings: Settings, client=None) -> CheckReport:
    kind = private_kind(settings)
    report = CheckReport(
        exchange=settings.exchange.name,
        desk_mode=settings.mode,
        exchange_mode=kind,
        keys_set=False,
    )
    if kind == "mainnet" and not settings.allow_mainnet:
        report.detail = "refusing: mainnet check needs mode: mainnet and allow_mainnet: true"
        report.exit_code = 2
        return report
    try:
        key_env, secret_env = credential_env_names(settings.exchange.name, kind)
    except ValueError as exc:
        report.detail = str(exc)
        report.exit_code = 2
        return report
    report.keys_set = keys_are_set(settings.exchange.name, kind)
    if not report.keys_set:
        report.detail = f"missing {key_env} or {secret_env}"
        report.exit_code = 1
        return report
    if client is None:
        import os

        client = make_trading_client(
            settings.exchange.name,
            os.environ[key_env].strip(),
            os.environ[secret_env].strip(),
            kind=kind,
            allow_mainnet=settings.allow_mainnet,
        )
    try:
        guard_client(client, kind=kind, name=settings.exchange.name, allow_mainnet=settings.allow_mainnet)
    except (DemoHostError, MainnetDisabled) as exc:
        report.detail = str(exc)
        report.exit_code = 2
        return report
    permissions = _read_permissions(client)
    if permissions is not None:
        report.read_only = permissions.read_only
        report.withdraw_permission = permissions.withdraw
    else:
        logger.warning("api key permission endpoint unavailable; withdraw flag is unknown")
    report.balances = _read_balances(client, unified=kind == "demo" and settings.exchange.name == "bybit")
    if report.withdraw_permission:
        report.detail = "refusing to run: withdrawal permission is enabled"
        report.exit_code = 2
    else:
        report.detail = "ok"
    return report


def _read_permissions(client) -> KeyPermissions | None:
    method = getattr(client, "privateGetV5UserQueryApi", None)
    if not callable(method):
        return None
    try:
        payload = method()
    except Exception:
        # The exception text can echo request headers. Log only the failure.
        logger.warning("api key info request failed")
        return None
    if not isinstance(payload, dict):
        return None
    return parse_key_permissions(payload)


def _read_balances(client, *, unified: bool) -> dict[str, float]:
    params = {"type": "unified"} if unified else {}
    try:
        balance = client.fetch_balance(params)
    except Exception:
        logger.warning("balance request failed")
        return {}
    if not isinstance(balance, dict):
        return {}
    return wallet_totals(balance)
