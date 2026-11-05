"""Brokers. Paper never sends orders. Testnet is sandbox-only.

Bybit demo trading uses https://api-demo.bybit.com and is not mainnet.
Mainnet construction raises unless ``allow_mainnet`` is true.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Protocol
from urllib.parse import urlparse

from dexbot.models import Candle, Fill, Ticker

logger = logging.getLogger("dexbot.execution")


class MainnetDisabled(RuntimeError):
    pass


class DemoHostError(RuntimeError):
    """Private Bybit demo client is not pinned to api-demo.bybit.com."""


DEMO_HOST = "api-demo.bybit.com"
DEMO_REST = "https://api-demo.bybit.com"
_BALANCE_META = {"info", "free", "used", "total", "timestamp", "datetime", "debt"}


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


def _resolved_host(url: str, hostname: str) -> str:
    text = (url or "").replace("{hostname}", hostname or "")
    if "://" not in text:
        text = "https://" + text
    return (urlparse(text).hostname or "").lower()


def assert_demo_urls(client: object) -> None:
    """Private demo REST must be api-demo.bybit.com, never the real mainnet host."""
    urls = getattr(client, "urls", {}) or {}
    api = urls.get("api") or {}
    if not isinstance(api, dict):
        raise DemoHostError("Bybit demo client has no API URL map")
    hostname = getattr(client, "hostname", "") or "bybit.com"
    for key in ("private", "spot"):
        host = _resolved_host(str(api.get(key, "")), hostname)
        if host != DEMO_HOST:
            raise DemoHostError(
                f"Bybit demo {key} host is {host or 'unset'}; refusing anything but {DEMO_HOST}"
            )


def configure_bybit_demo(client) -> str:
    """Pin a Bybit client at demo trading. Returns ``ccxt`` or ``url_override``."""
    method = getattr(client, "enable_demo_trading", None)
    if callable(method):
        method(True)
        how = "ccxt"
    else:
        _force_demo_urls(client)
        how = "url_override"
    options = getattr(client, "options", None)
    if isinstance(options, dict):
        options["enableDemoTrading"] = True
        options["enableUnifiedAccount"] = True
        options["enableUnifiedMargin"] = False
        options["unifiedMarginStatus"] = 6
        options["defaultType"] = "spot"
    assert_demo_urls(client)
    return how


def _force_demo_urls(client) -> None:
    api = (getattr(client, "urls", {}) or {}).get("api")
    if isinstance(api, dict):
        client.urls["api"] = {key: DEMO_REST for key in api}
    else:
        client.urls["api"] = DEMO_REST
    if not getattr(client, "hostname", None):
        client.hostname = "bybit.com"


def guard_client(client, *, kind: str, name: str, allow_mainnet: bool) -> None:
    """Refuse a private call that is not on the venue the operator selected."""
    if kind == "demo":
        if name != "bybit":
            raise DemoHostError("demo trading is Bybit only")
        assert_demo_urls(client)
        return
    if kind == "testnet":
        assert_sandbox_urls(name, client)
        return
    if kind == "mainnet":
        if not allow_mainnet:
            raise MainnetDisabled(
                "Refusing mainnet orders. Set mode: mainnet and allow_mainnet: true together."
            )
        return
    raise ValueError(f"unsupported venue {kind}")


def wallet_totals(balance: dict) -> dict[str, float]:
    """USDT/USDC/BTC/ETH first, then any other non-zero coin. Ignores raw payloads."""
    found: dict[str, float] = {}

    def take(code: str, entry: object) -> None:
        if not isinstance(entry, dict):
            return
        if "free" not in entry and "total" not in entry and "used" not in entry:
            return
        total = entry.get("total")
        if total is None:
            total = float(entry.get("free") or 0) + float(entry.get("used") or 0)
        found[code] = float(total)

    for code in ("USDT", "USDC", "BTC", "ETH"):
        if code in balance:
            take(code, balance[code])
    for code, entry in balance.items():
        if code in found or code in _BALANCE_META or not isinstance(code, str):
            continue
        if isinstance(entry, dict) and "total" in entry and float(entry.get("total") or 0) != 0:
            take(code, entry)
    return found


def quote_total(balance: dict, quote: str = "USDT") -> float:
    return float(wallet_totals(balance).get(quote, 0.0))


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


def make_trading_client(name: str, key: str, secret: str, *, kind: str, allow_mainnet: bool):
    """Build a private ccxt client. ``kind`` is demo, testnet, or mainnet.

    Keys are passed to ccxt and never logged. Demo never calls ``set_sandbox_mode``
    and never leaves the private host on api.bybit.com.
    """
    if kind not in {"demo", "testnet", "mainnet"}:
        raise ValueError(f"unsupported venue {kind}")
    if kind == "mainnet" and not allow_mainnet:
        raise MainnetDisabled(
            "Refusing mainnet orders. Set mode: mainnet and allow_mainnet: true together. "
            "Paper, demo, and testnet do not use this flag."
        )
    if kind == "demo" and name != "bybit":
        raise DemoHostError("demo trading is Bybit only")
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
    if kind == "testnet":
        client.set_sandbox_mode(True)
        assert_sandbox_urls(name, client)
    elif kind == "demo":
        configure_bybit_demo(client)
        guard_client(client, kind="demo", name=name, allow_mainnet=False)
    else:
        logger.critical(
            "MAINNET orders enabled via allow_mainnet on %s. This can move real funds.",
            name,
        )
    return client


def make_public_client(name: str, *, demo: bool = False):
    """Keyless market-data client. It has no order credentials."""
    cls = _exchange_class(name)
    client = cls({"enableRateLimit": True, "timeout": 20_000, "options": {"defaultType": "spot"}})
    if demo:
        if name != "bybit":
            raise DemoHostError("demo market data probe is Bybit only")
        configure_bybit_demo(client)
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
    def __init__(self, client, *, kind: str, exchange_name: str, allow_mainnet: bool) -> None:
        self.client = client
        self.kind = kind
        self.exchange_name = exchange_name
        self.allow_mainnet = allow_mainnet
        self._marks: dict[str, float] = {}
        self._guard()

    def set_marks(self, marks: dict[str, float]) -> None:
        self._marks = dict(marks)

    def mark(self, symbol: str) -> float:
        if symbol in self._marks:
            return float(self._marks[symbol])
        ticker = self.client.fetch_ticker(symbol)
        return float(ticker["last"])

    def get_cash(self) -> float:
        self._guard()
        # Bybit demo accounts are unified. Ask for that wallet explicitly.
        params = {"type": "unified"} if self.kind == "demo" and self.exchange_name == "bybit" else {}
        balance = self.client.fetch_balance(params)
        return quote_total(balance, "USDT")

    def buy(self, symbol: str, qty_base: float) -> Fill:
        self._guard()
        return self._order(symbol, "buy", qty_base)

    def sell(self, symbol: str, qty_base: float) -> Fill:
        self._guard()
        return self._order(symbol, "sell", qty_base)

    def asset_balances(self) -> dict[str, float]:
        """Base-coin totals from the same wallet orders use. Missing coins are absent."""
        self._guard()
        params = {"type": "unified"} if self.kind == "demo" and self.exchange_name == "bybit" else {}
        balance = self.client.fetch_balance(params)
        return wallet_totals(balance)

    def place_protection(self, symbol: str, qty_base: float, stop: float, take_profit: float) -> list[dict]:
        """Bybit spot conditional TP/SL, separate from the market buy.

        ccxt refuses to attach stopLoss/takeProfit to a spot market order.
        Bybit accepts a follow-up ``tpslOrder`` (triggerDirection 2 = stop, 1 = take-profit).
        A rejected leg is logged and skipped; the bot loop still checks stops.
        """
        self._guard()
        if self.exchange_name != "bybit":
            logger.info(
                "exchange protective stops are not implemented for %s; the hourly loop still enforces them",
                self.exchange_name,
            )
            return []
        submit = getattr(self.client, "privatePostV5OrderCreate", None)
        if not callable(submit):
            logger.warning("bybit client cannot place v5 spot tpsl orders; loop stops remain")
            return []
        placed: list[dict] = []
        legs = (
            ("exchange_stop", stop, 2),
            ("exchange_take_profit", take_profit, 1),
        )
        for reason, trigger, direction in legs:
            body = {
                "category": "spot",
                "symbol": _bybit_symbol_id(self.client, symbol),
                "side": "Sell",
                "orderType": "Market",
                "qty": _amount_string(self.client, symbol, qty_base),
                "triggerPrice": _price_string(self.client, symbol, trigger),
                "triggerDirection": direction,
                "orderFilter": "tpslOrder",
                "triggerBy": "LastPrice",
            }
            try:
                response = submit(body)
            except Exception:
                logger.warning("protective %s request failed for %s", reason, symbol)
                continue
            order_id = _bybit_order_id(response)
            if not order_id:
                code = response.get("retCode") if isinstance(response, dict) else None
                logger.warning("protective %s rejected for %s code=%s", reason, symbol, code)
                continue
            placed.append(
                {
                    "reason": reason,
                    "order_id": order_id,
                    "symbol": symbol,
                    "qty_base": qty_base,
                    "trigger": trigger,
                }
            )
        return placed

    def _guard(self) -> None:
        guard_client(
            self.client,
            kind=self.kind,
            name=self.exchange_name,
            allow_mainnet=self.allow_mainnet,
        )

    def _order(self, symbol: str, side: str, qty_base: float) -> Fill:
        qty = float(self.client.amount_to_precision(symbol, qty_base))
        if qty <= 0:
            raise RuntimeError("amount rounded to zero")
        params = {"category": "spot"} if self.kind == "demo" and self.exchange_name == "bybit" else {}
        if side == "buy":
            order = self.client.create_market_buy_order(symbol, qty, params)
        else:
            order = self.client.create_market_sell_order(symbol, qty, params)
        return _fill_from_ccxt(order, symbol, side, qty, self._marks.get(symbol, 0.0))


def _bybit_symbol_id(client, symbol: str) -> str:
    market_fn = getattr(client, "market", None)
    if callable(market_fn):
        try:
            market = market_fn(symbol)
        except Exception:
            market = None
        if isinstance(market, dict) and market.get("id"):
            return str(market["id"])
    return symbol.replace("/", "")


def _amount_string(client, symbol: str, qty: float) -> str:
    precise = getattr(client, "amount_to_precision", None)
    if callable(precise):
        return str(precise(symbol, qty))
    return str(qty)


def _price_string(client, symbol: str, price: float) -> str:
    precise = getattr(client, "price_to_precision", None)
    if callable(precise):
        return str(precise(symbol, price))
    return str(price)


def _bybit_order_id(response: object) -> str | None:
    if not isinstance(response, dict):
        return None
    if str(response.get("retCode", 0)) != "0":
        return None
    result = response.get("result") or {}
    if not isinstance(result, dict):
        return None
    order_id = result.get("orderId") or result.get("order_id")
    return str(order_id) if order_id else None


def flat_symbols(positions: list, balances: dict[str, float], *, dust_ratio: float = 0.5) -> list:
    """Journal positions the exchange no longer holds (stop filled while we were down)."""
    gone = []
    for pos in positions:
        base = pos.symbol.split("/")[0]
        held = float(balances.get(base, 0.0))
        if held + 1e-12 < float(pos.qty_base) * dust_ratio:
            gone.append(pos)
    return gone


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
        client = make_public_client(exchange_name, demo=False)
        super().__init__(client, quote=quote, timeframe=timeframe, limit=limit)


class SwitchingMarketData:
    """Try the demo public book, then keyless mainnet public data.

    The fallback client has no API key, so a miss cannot place a mainnet order.
    """

    def __init__(self, primary: MarketData, secondary: MarketData) -> None:
        self.primary = primary
        self.secondary = secondary
        self.using_secondary = False

    def fetch_tickers(self) -> list[Ticker]:
        if self.using_secondary:
            return self.secondary.fetch_tickers()
        try:
            rows = self.primary.fetch_tickers()
        except Exception:
            logger.warning("demo public tickers unavailable; using mainnet public market data")
            self.using_secondary = True
            return self.secondary.fetch_tickers()
        if not rows:
            logger.warning("demo public tickers empty; using mainnet public market data")
            self.using_secondary = True
            return self.secondary.fetch_tickers()
        return rows

    def fetch_ohlcv(self, symbol: str) -> list[Candle]:
        if self.using_secondary:
            return self.secondary.fetch_ohlcv(symbol)
        try:
            rows = self.primary.fetch_ohlcv(symbol)
        except Exception:
            logger.warning("demo public ohlcv unavailable for %s; using mainnet public candles", symbol)
            self.using_secondary = True
            return self.secondary.fetch_ohlcv(symbol)
        if not rows:
            self.using_secondary = True
            return self.secondary.fetch_ohlcv(symbol)
        return rows
