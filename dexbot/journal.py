"""SQLite journal of candidates, agent votes, orders, fills, and PnL."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import sqlite3

from dexbot.models import Fill, Position


def _dumps(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False, default=str)


def _iso(moment: datetime) -> str:
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc).isoformat(timespec="seconds")


class Journal:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.execute("PRAGMA journal_mode=WAL")
        self._schema()
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    def _schema(self) -> None:
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS runs (
                id INTEGER PRIMARY KEY,
                started_at TEXT NOT NULL,
                finished_at TEXT,
                mode TEXT NOT NULL,
                exchange TEXT NOT NULL,
                mock INTEGER NOT NULL,
                status TEXT NOT NULL,
                note TEXT
            );
            CREATE TABLE IF NOT EXISTS candidates (
                id INTEGER PRIMARY KEY,
                run_id INTEGER NOT NULL,
                symbol TEXT NOT NULL,
                quote_volume_24h REAL,
                spread_bps REAL,
                last_price REAL,
                passed INTEGER NOT NULL,
                reject_reason TEXT,
                payload_json TEXT,
                FOREIGN KEY (run_id) REFERENCES runs(id)
            );
            CREATE TABLE IF NOT EXISTS agent_outputs (
                id INTEGER PRIMARY KEY,
                run_id INTEGER NOT NULL,
                symbol TEXT NOT NULL,
                agent TEXT NOT NULL,
                stance TEXT,
                reasoning TEXT,
                payload_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY (run_id) REFERENCES runs(id)
            );
            CREATE TABLE IF NOT EXISTS decisions (
                id INTEGER PRIMARY KEY,
                run_id INTEGER NOT NULL,
                symbol TEXT NOT NULL,
                action TEXT NOT NULL,
                skeptic_vote TEXT,
                executed INTEGER NOT NULL,
                block_reason TEXT,
                payload_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY (run_id) REFERENCES runs(id)
            );
            CREATE TABLE IF NOT EXISTS orders (
                id INTEGER PRIMARY KEY,
                run_id INTEGER,
                decision_id INTEGER,
                symbol TEXT NOT NULL,
                side TEXT NOT NULL,
                qty_base REAL,
                qty_quote REAL,
                price REAL,
                status TEXT NOT NULL,
                exchange_order_id TEXT,
                mode TEXT NOT NULL,
                reason TEXT,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS fills (
                id INTEGER PRIMARY KEY,
                order_id INTEGER NOT NULL,
                symbol TEXT NOT NULL,
                side TEXT NOT NULL,
                price REAL NOT NULL,
                qty_base REAL NOT NULL,
                fee_quote REAL NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY (order_id) REFERENCES orders(id)
            );
            CREATE TABLE IF NOT EXISTS positions (
                id INTEGER PRIMARY KEY,
                symbol TEXT NOT NULL,
                qty_base REAL NOT NULL,
                avg_entry REAL NOT NULL,
                stop_loss REAL NOT NULL,
                take_profit REAL NOT NULL,
                buy_fee REAL NOT NULL,
                opened_at TEXT NOT NULL,
                closed_at TEXT,
                realized_pnl REAL NOT NULL DEFAULT 0,
                status TEXT NOT NULL,
                entry_run_id INTEGER NOT NULL,
                last_mark REAL
            );
            CREATE TABLE IF NOT EXISTS equity_snapshots (
                id INTEGER PRIMARY KEY,
                ts TEXT NOT NULL,
                equity REAL NOT NULL,
                day TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS account (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                cash REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_agent_run ON agent_outputs(run_id, symbol, agent);
            CREATE INDEX IF NOT EXISTS idx_pos_status ON positions(status);
            """
        )

    def start_run(self, *, mode: str, exchange: str, mock: bool, started_at: datetime) -> int:
        cur = self.conn.execute(
            """
            INSERT INTO runs (started_at, mode, exchange, mock, status)
            VALUES (?, ?, ?, ?, 'running')
            """,
            (_iso(started_at), mode, exchange, int(mock)),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def finish_run(self, run_id: int, status: str, note: str = "", finished_at: datetime | None = None) -> None:
        self.conn.execute(
            "UPDATE runs SET status=?, note=?, finished_at=? WHERE id=?",
            (status, note[:500], _iso(finished_at or datetime.now(timezone.utc)), run_id),
        )
        self.conn.commit()

    def add_candidate(
        self,
        run_id: int,
        *,
        symbol: str,
        quote_volume: float,
        spread_bps: float | None,
        last_price: float,
        passed: bool,
        reject_reason: str | None,
        payload: dict,
    ) -> None:
        self.conn.execute(
            """
            INSERT INTO candidates (
                run_id, symbol, quote_volume_24h, spread_bps, last_price,
                passed, reject_reason, payload_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run_id,
                symbol,
                quote_volume,
                spread_bps,
                last_price,
                int(passed),
                reject_reason,
                _dumps(payload),
            ),
        )
        self.conn.commit()

    def add_agent_output(
        self,
        run_id: int,
        *,
        symbol: str,
        agent: str,
        stance: str,
        reasoning: str,
        payload: dict,
        created_at: datetime,
    ) -> None:
        self.conn.execute(
            """
            INSERT INTO agent_outputs (
                run_id, symbol, agent, stance, reasoning, payload_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (run_id, symbol, agent, stance, reasoning, _dumps(payload), _iso(created_at)),
        )
        self.conn.commit()

    def add_decision(
        self,
        run_id: int,
        *,
        symbol: str,
        action: str,
        skeptic_vote: str | None,
        executed: bool,
        block_reason: str | None,
        payload: dict,
        created_at: datetime,
    ) -> int:
        cur = self.conn.execute(
            """
            INSERT INTO decisions (
                run_id, symbol, action, skeptic_vote, executed, block_reason,
                payload_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run_id,
                symbol,
                action,
                skeptic_vote,
                int(executed),
                block_reason,
                _dumps(payload),
                _iso(created_at),
            ),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def record_entry(
        self,
        *,
        run_id: int,
        decision_id: int,
        mode: str,
        fill: Fill,
        stop_loss: float,
        take_profit: float,
        created_at: datetime,
        cash: float | None,
    ) -> int:
        try:
            order_id = self._insert_order(
                run_id=run_id,
                decision_id=decision_id,
                mode=mode,
                fill=fill,
                reason="entry",
                created_at=created_at,
            )
            self._insert_fill(order_id, fill, created_at)
            cur = self.conn.execute(
                """
                INSERT INTO positions (
                    symbol, qty_base, avg_entry, stop_loss, take_profit, buy_fee,
                    opened_at, realized_pnl, status, entry_run_id, last_mark
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 0, 'open', ?, ?)
                """,
                (
                    fill.symbol,
                    fill.qty_base,
                    fill.price,
                    stop_loss,
                    take_profit,
                    fill.fee_quote,
                    _iso(created_at),
                    run_id,
                    fill.price,
                ),
            )
            if cash is not None:
                self.conn.execute("UPDATE account SET cash=? WHERE id=1", (cash,))
            self.conn.commit()
            return int(cur.lastrowid)
        except Exception:
            self.conn.rollback()
            raise

    def record_exit(
        self,
        *,
        run_id: int,
        position_id: int,
        mode: str,
        fill: Fill,
        reason: str,
        created_at: datetime,
        cash: float | None,
    ) -> float:
        row = self.conn.execute("SELECT * FROM positions WHERE id=?", (position_id,)).fetchone()
        if row is None:
            raise KeyError(position_id)
        pnl = (fill.price - float(row["avg_entry"])) * float(row["qty_base"]) - fill.fee_quote - float(row["buy_fee"])
        try:
            order_id = self._insert_order(
                run_id=run_id,
                decision_id=None,
                mode=mode,
                fill=fill,
                reason=reason,
                created_at=created_at,
            )
            self._insert_fill(order_id, fill, created_at)
            self.conn.execute(
                """
                UPDATE positions
                SET status='closed', closed_at=?, realized_pnl=?, last_mark=?
                WHERE id=?
                """,
                (_iso(created_at), pnl, fill.price, position_id),
            )
            if cash is not None:
                self.conn.execute("UPDATE account SET cash=? WHERE id=1", (cash,))
            self.conn.commit()
            return pnl
        except Exception:
            self.conn.rollback()
            raise

    def _insert_order(
        self,
        *,
        run_id: int,
        decision_id: int | None,
        mode: str,
        fill: Fill,
        reason: str,
        created_at: datetime,
    ) -> int:
        cur = self.conn.execute(
            """
            INSERT INTO orders (
                run_id, decision_id, symbol, side, qty_base, qty_quote, price,
                status, exchange_order_id, mode, reason, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run_id,
                decision_id,
                fill.symbol,
                fill.side,
                fill.qty_base,
                fill.qty_quote,
                fill.price,
                fill.status,
                fill.order_id,
                mode,
                reason,
                _iso(created_at),
            ),
        )
        return int(cur.lastrowid)

    def _insert_fill(self, order_id: int, fill: Fill, created_at: datetime) -> None:
        self.conn.execute(
            """
            INSERT INTO fills (order_id, symbol, side, price, qty_base, fee_quote, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (order_id, fill.symbol, fill.side, fill.price, fill.qty_base, fill.fee_quote, _iso(created_at)),
        )

    def open_positions(self) -> list[Position]:
        rows = self.conn.execute(
            "SELECT * FROM positions WHERE status='open' ORDER BY id"
        ).fetchall()
        return [self._position(row) for row in rows]

    def open_count(self) -> int:
        row = self.conn.execute(
            "SELECT COUNT(*) AS n FROM positions WHERE status='open'"
        ).fetchone()
        return int(row["n"])

    def has_open(self, symbol: str) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM positions WHERE status='open' AND symbol=? LIMIT 1",
            (symbol,),
        ).fetchone()
        return row is not None

    def mark_positions(self, marks: dict[str, float]) -> None:
        for pos in self.open_positions():
            if pos.symbol in marks:
                self.conn.execute(
                    "UPDATE positions SET last_mark=? WHERE id=?",
                    (marks[pos.symbol], pos.id),
                )
        self.conn.commit()

    def ensure_cash(self, initial: float) -> float:
        row = self.conn.execute("SELECT cash FROM account WHERE id=1").fetchone()
        if row is None:
            self.conn.execute("INSERT INTO account (id, cash) VALUES (1, ?)", (initial,))
            self.conn.commit()
            return float(initial)
        return float(row["cash"])

    def set_cash(self, cash: float) -> None:
        self.ensure_cash(cash)
        self.conn.execute("UPDATE account SET cash=? WHERE id=1", (cash,))
        self.conn.commit()

    def get_cash(self) -> float | None:
        row = self.conn.execute("SELECT cash FROM account WHERE id=1").fetchone()
        return None if row is None else float(row["cash"])

    def snapshot_equity(self, moment: datetime, equity: float) -> None:
        day = _iso(moment)[:10]
        self.conn.execute(
            "INSERT INTO equity_snapshots (ts, equity, day) VALUES (?, ?, ?)",
            (_iso(moment), equity, day),
        )
        self.conn.commit()

    def day_start_equity(self, moment: datetime) -> float | None:
        day = _iso(moment)[:10]
        row = self.conn.execute(
            "SELECT equity FROM equity_snapshots WHERE day=? ORDER BY id ASC LIMIT 1",
            (day,),
        ).fetchone()
        return None if row is None else float(row["equity"])

    def decisions(self) -> list[dict]:
        return [dict(row) for row in self.conn.execute("SELECT * FROM decisions ORDER BY id")]

    def orders(self) -> list[dict]:
        return [dict(row) for row in self.conn.execute("SELECT * FROM orders ORDER BY id")]

    def agent_rows(self) -> list[dict]:
        return [dict(row) for row in self.conn.execute("SELECT * FROM agent_outputs ORDER BY id")]

    def closed_positions(self) -> list[Position]:
        rows = self.conn.execute(
            "SELECT * FROM positions WHERE status='closed' ORDER BY id"
        ).fetchall()
        return [self._position(row) for row in rows]

    @staticmethod
    def _position(row: sqlite3.Row) -> Position:
        return Position(
            id=int(row["id"]),
            symbol=row["symbol"],
            qty_base=float(row["qty_base"]),
            avg_entry=float(row["avg_entry"]),
            stop_loss=float(row["stop_loss"]),
            take_profit=float(row["take_profit"]),
            buy_fee=float(row["buy_fee"]),
            opened_at=row["opened_at"],
            entry_run_id=int(row["entry_run_id"]),
            last_mark=float(row["last_mark"] if row["last_mark"] is not None else row["avg_entry"]),
            status=row["status"],
            realized_pnl=float(row["realized_pnl"] or 0),
            closed_at=row["closed_at"],
        )
