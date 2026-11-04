"""Brokers. Paper never sends orders. Testnet is sandbox-only.

Mainnet construction raises unless ``allow_mainnet`` is true.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Protocol

from dexbot.models import Candle, Fill, Ticker

logger = logging.getLogger("dexbot.execution")


class MainnetDisabled(RuntimeError):
    pass


class MarketData(Protocol):
    def fetch_tickers(self) -> list[Ticker]: ...

    def fetch_ohlcv(self, symbol: str) -> list[Candle]: ...


class Broker(Protocol):
    def set_marks(self, marks: dict[str, float]) -> None: ...

    def mark(self, symbol: str) -> float: ...

    def get_cash(self) -> float: ...

    def buy(self, symbol: str, qty_base: float) -> Fill: ...

    def sell(self, symbol: str, qty_base: float) -> Fill: ...


@dataclass
class Venue:
    market: MarketData
    broker: Broker
    name: str


class PaperBroker:
    """Fills at the marked last price. Cash is the paper account."""

    def __init__(self, cash: float, fee_rate: float) -> None:
        self.cash = float(cash)
        self.fee_rate = fee_rate
        self._marks: dict[str, float] = {}
        self._seq = 0

    def set_marks(self, marks: dict[str, float]) -> None:
        self._marks = dict(marks)

    def mark(self, symbol: str) -> float:
        if symbol not in self._marks:
            raise RuntimeError(f"no mark for {symbol}")
        return float(self._marks[symbol])

    def get_cash(self) -> float:
        return self.cash

    def buy(self, symbol: str, qty_base: float) -> Fill:
        price = self.mark(symbol)
        if price <= 0 or qty_base <= 0:
            raise RuntimeError("bad paper buy")
        quote = qty_base * price
        fee = quote * self.fee_rate
        if self.cash + 1e-8 < quote + fee:
            raise RuntimeError("insufficient paper cash")
        self.cash -= quote + fee
        return self._fill(symbol, "buy", price, qty_base, quote, fee)

    def sell(self, symbol: str, qty_base: float) -> Fill:
        price = self.mark(symbol)
        if price <= 0 or qty_base <= 0:
            raise RuntimeError("bad paper sell")
        quote = qty_base * price
        fee = quote * self.fee_rate
        self.cash += quote - fee
        return self._fill(symbol, "sell", price, qty_base, quote, fee)

    def _fill(self, symbol: str, side: str, price: float, qty: float, quote: float, fee: float) -> Fill:
        self._seq += 1
        return Fill(
            order_id=f"paper-{self._seq}",
            symbol=symbol,
            side=side,
            price=price,
            qty_base=qty,
            qty_quote=quote,
            fee_quote=fee,
            status="filled",
        )


class FixtureMarket:
    def __init__(self, tickers: list[Ticker], candles: dict[str, list[Candle]]) -> None:
        self.tickers = tickers
        self.candles = candles

    def fetch_tickers(self) -> list[Ticker]:
        return list(self.tickers)

    def fetch_ohlcv(self, symbol: str) -> list[Candle]:
        return list(self.candles.get(symbol, []))


def _exchange_class(name: str):
    import ccxt

    if name == "bybit":
        return ccxt.bybit
    if name == "binance":
        return ccxt.binance
    raise ValueError(f"unsupported exchange {name}")


def assert_sandbox_urls(name: str, client: object) -> None:
    """Refuse to trade if sandbox mode did not retarget the spot REST host."""
    urls = getattr(client, "urls", {}) or {}
    api = urls.get("api") or {}
    if name == "bybit":
        spot = api.get("spot") if isinstance(api, dict) else str(api)
        if "testnet" not in str(spot):
            raise RuntimeError(f"Bybit sandbox URL missing testnet host: {spot}")
        return
    if name == "binance":
        public = api.get("public", "") if isinstance(api, dict) else ""
        private = api.get("private", "") if isinstance(api, dict) else ""
        if "testnet.binance.vision" not in str(public) or "testnet.binance.vision" not in str(private):
            raise RuntimeError(
                "Binance sandbox spot URL is not testnet.binance.vision: "
                + json.dumps({"public": public, "private": private})
            )
        return
    raise ValueError(f"unsupported exchange {name}")


def make_trading_client(name: str, key: str, secret: str, *, sandbox: bool, allow_mainnet: bool):
    if not sandbox and not allow_mainnet:
        raise MainnetDisabled(
            "Refusing mainnet orders. Set mode: mainnet and allow_mainnet: true together. "
            "Paper and testnet do not use this flag."
        )
    cls = _exchange_class(name)
    client = cls(
        {
            "apiKey": key,
            "secret": secret,
            "enableRateLimit": True,
            "timeout": 20_000,
            "options": {"defaultType": "spot"},
        }
    )
    if sandbox:
        client.set_sandbox_mode(True)
        assert_sandbox_urls(name, client)
    else:
        logger.critical(
            "MAINNET orders enabled via allow_mainnet on %s. This can move real funds.",
            name,
        )
    return client


def _listing_from_market(market: dict) -> datetime | None:
    info = market.get("info") or {}
    raw_values = []
    for key in ("launchTime", "onboardDate", "listingTime", "created"):
        if info.get(key):
            raw_values.append(info.get(key))
    if market.get("created"):
        raw_values.append(market.get("created"))
    for raw in raw_values:
        try:
            value = int(float(raw))
        except (TypeError, ValueError):
            continue
        if value > 10_000_000_000:
            value = value / 1000.0
        try:
            return datetime.fromtimestamp(value, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            continue
    return None


def tickers_from_ccxt(client, *, quote: str) -> list[Ticker]:
    markets = client.load_markets()
    raw = client.fetch_tickers()
    listed: list[Ticker] = []
    for symbol, ticker in raw.items():
        market = markets.get(symbol) or {}
        if market.get("spot") is False:
            continue
        if market.get("active") is False:
            continue
        if ":" in symbol:
            continue
        market_quote = (market.get("quote") or "").upper()
        if market_quote != quote.upper():
            continue
        last = ticker.get("last")
        if last is None:
            continue
        quote_volume = ticker.get("quoteVolume")
        if quote_volume is None and ticker.get("baseVolume") is not None:
            quote_volume = float(ticker["baseVolume"]) * float(last)
        if quote_volume is None:
            continue
        bid = ticker.get("bid") or 0.0
        ask = ticker.get("ask") or 0.0
        listed.append(
            Ticker(
                symbol=symbol,
                base=str(market.get("base") or symbol.split("/")[0]),
                quote=str(market.get("quote") or quote),
                last=float(last),
                bid=float(bid or 0.0),
                ask=float(ask or 0.0),
                quote_volume_24h=float(quote_volume),
                spot=True,
                active=True,
                listed_at=_listing_from_market(market),
            )
        )
    return listed


def candles_from_ccxt(client, symbol: str, *, timeframe: str, limit: int) -> list[Candle]:
    rows = client.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)
    candles = [
        Candle(
            ts=int(row[0]),
            open=float(row[1]),
            high=float(row[2]),
            low=float(row[3]),
            close=float(row[4]),
            volume=float(row[5]),
        )
        for row in rows
        if row and len(row) >= 6
    ]
    candles.sort(key=lambda candle: candle.ts)
    return candles


class CcxtMarketData:
    def __init__(self, client, *, quote: str, timeframe: str, limit: int) -> None:
        self.client = client
        self.quote = quote
        self.timeframe = timeframe
        self.limit = limit

    def fetch_tickers(self) -> list[Ticker]:
        return tickers_from_ccxt(self.client, quote=self.quote)

    def fetch_ohlcv(self, symbol: str) -> list[Candle]:
        return candles_from_ccxt(self.client, symbol, timeframe=self.timeframe, limit=self.limit)


class CcxtBroker:
    def __init__(self, client, *, sandbox: bool, exchange_name: str) -> None:
        self.client = client
        self.sandbox = sandbox
        self.exchange_name = exchange_name
        self._marks: dict[str, float] = {}

    def set_marks(self, marks: dict[str, float]) -> None:
        self._marks = dict(marks)

    def mark(self, symbol: str) -> float:
        if symbol in self._marks:
            return float(self._marks[symbol])
        ticker = self.client.fetch_ticker(symbol)
        return float(ticker["last"])

    def get_cash(self) -> float:
        balance = self.client.fetch_balance()
        usdt = balance.get("USDT") or {}
        total = usdt.get("total")
        if total is None:
            total = float(usdt.get("free") or 0) + float(usdt.get("used") or 0)
        return float(total or 0)

    def buy(self, symbol: str, qty_base: float) -> Fill:
        self._guard()
        return self._order(symbol, "buy", qty_base)

    def sell(self, symbol: str, qty_base: float) -> Fill:
        self._guard()
        return self._order(symbol, "sell", qty_base)

    def _guard(self) -> None:
        if self.sandbox:
            assert_sandbox_urls(self.exchange_name, self.client)

    def _order(self, symbol: str, side: str, qty_base: float) -> Fill:
        qty = float(self.client.amount_to_precision(symbol, qty_base))
        if qty <= 0:
            raise RuntimeError("amount rounded to zero")
        if side == "buy":
            order = self.client.create_market_buy_order(symbol, qty)
        else:
            order = self.client.create_market_sell_order(symbol, qty)
        return _fill_from_ccxt(order, symbol, side, qty, self._marks.get(symbol, 0.0))


def _fill_from_ccxt(order: dict, symbol: str, side: str, fallback_qty: float, mark: float) -> Fill:
    qty = float(order.get("filled") or 0) or fallback_qty
    price = float(order.get("average") or order.get("price") or mark or 0)
    cost = order.get("cost")
    quote = float(cost) if cost else qty * price
    fee_info = order.get("fee") or {}
    fee = float(fee_info.get("cost") or 0) if isinstance(fee_info, dict) else 0.0
    if qty <= 0 or price <= 0:
        raise RuntimeError(f"order not filled: {order.get('id')} status={order.get('status')}")
    return Fill(
        order_id=str(order.get("id") or ""),
        symbol=symbol,
        side=side,
        price=price,
        qty_base=qty,
        qty_quote=quote,
        fee_quote=fee,
        status=str(order.get("status") or "closed"),
    )


class PublicMarketData(CcxtMarketData):
    """Read-only public market data. This object cannot place orders.

    Paper mode uses production public REST so marks match the live book.
    No API key is attached. Order entry stays on PaperBroker.
    """

    def __init__(self, exchange_name: str, *, quote: str, timeframe: str, limit: int) -> None:
        cls = _exchange_class(exchange_name)
        client = cls({"enableRateLimit": True, "timeout": 20_000, "options": {"defaultType": "spot"}})
        super().__init__(client, quote=quote, timeframe=timeframe, limit=limit)
