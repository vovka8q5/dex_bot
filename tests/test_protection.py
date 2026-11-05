from datetime import datetime, timezone
from types import SimpleNamespace

from dexbot.execution import CcxtBroker, flat_symbols
from dexbot.journal import Journal
from dexbot.models import Fill
from dexbot.pipeline import _reconcile_flat


class _Bybit:
    hostname = "bybit.com"
    urls = {
        "api": {
            "private": "https://api-demo.bybit.com",
            "spot": "https://api-demo.bybit.com",
        }
    }

    def __init__(self) -> None:
        self.buys: list[tuple] = []
        self.protections: list[dict] = []

    def amount_to_precision(self, symbol, qty):
        return f"{float(qty):.4f}"

    def price_to_precision(self, symbol, price):
        return f"{float(price):.2f}"

    def create_market_buy_order(self, symbol, qty, params=None):
        self.buys.append((symbol, qty, params or {}))
        return {
            "id": "mkt-1",
            "filled": qty,
            "average": 100,
            "cost": float(qty) * 100,
            "status": "closed",
            "fee": {"cost": 0},
        }

    def privatePostV5OrderCreate(self, body):
        self.protections.append(body)
        return {"retCode": 0, "result": {"orderId": f"p-{len(self.protections)}"}}


def test_bybit_protection_is_two_spot_tpsl_orders_not_attached_to_the_buy():
    client = _Bybit()
    broker = CcxtBroker(client, kind="demo", exchange_name="bybit", allow_mainnet=False)
    broker.set_marks({"BTC/USDT": 100})
    broker.buy("BTC/USDT", 0.25)
    placed = broker.place_protection("BTC/USDT", 0.25, 95, 110)
    assert client.buys[0][2] == {"category": "spot"}
    assert "stopLoss" not in client.buys[0][2]
    assert [item["reason"] for item in placed] == ["exchange_stop", "exchange_take_profit"]
    stop, take = client.protections
    assert stop["orderFilter"] == "tpslOrder"
    assert stop["category"] == "spot"
    assert stop["side"] == "Sell"
    assert stop["symbol"] == "BTCUSDT"
    assert stop["triggerDirection"] == 2
    assert stop["triggerPrice"] == "95.00"
    assert "reduceOnly" not in stop
    assert take["triggerDirection"] == 1
    assert take["triggerPrice"] == "110.00"


def test_flat_symbols_when_the_exchange_no_longer_holds_the_coin():
    class Pos:
        def __init__(self, symbol, qty):
            self.symbol = symbol
            self.qty_base = qty

    open_rows = [Pos("BTC/USDT", 1.0), Pos("ETH/USDT", 2.0)]
    gone = flat_symbols(open_rows, {"BTC": 0.0, "ETH": 2.0})
    assert [row.symbol for row in gone] == ["BTC/USDT"]


def test_reconcile_closes_a_vanished_position_without_selling(tmp_path):
    journal = Journal(tmp_path / "journal.sqlite")
    now = datetime(2026, 11, 5, tzinfo=timezone.utc)
    run_id = journal.start_run(mode="demo", exchange="bybit-demo", mock=False, started_at=now)
    decision_id = journal.add_decision(
        run_id,
        symbol="BTC/USDT",
        action="buy",
        skeptic_vote="buy",
        executed=True,
        block_reason=None,
        payload={},
        created_at=now,
    )
    journal.record_entry(
        run_id=run_id,
        decision_id=decision_id,
        mode="demo",
        fill=Fill("buy-1", "BTC/USDT", "buy", 100, 1, 100, 0, "filled"),
        stop_loss=95,
        take_profit=110,
        created_at=now,
        cash=None,
    )

    class Broker:
        def __init__(self):
            self.sells = 0

        def asset_balances(self):
            return {"BTC": 0.0}

        def mark(self, symbol):
            return 96.0

        def sell(self, symbol, qty):
            self.sells += 1
            raise AssertionError("already flat")

    broker = Broker()
    runtime = SimpleNamespace(
        venue=SimpleNamespace(broker=broker),
        journal=journal,
        settings=SimpleNamespace(mode="demo"),
    )
    try:
        closed = _reconcile_flat(runtime, run_id, now)
        assert closed == 1
        assert broker.sells == 0
        assert journal.open_count() == 0
        assert journal.orders()[-1]["reason"] == "reconciled_flat"
    finally:
        journal.close()
