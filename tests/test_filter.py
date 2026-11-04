from datetime import datetime, timedelta, timezone

import pytest

from dexbot.config import FilterConfig
from dexbot.filter import filter_tokens, is_leveraged_base, spread_bps
from dexbot.market import Candle, drop_forming, make_candles, volume_signal
from dexbot.models import Ticker


def _cfg(**kwargs) -> FilterConfig:
    base = dict(
        top_n=3,
        top_n_min=1,
        top_n_max=10,
        min_quote_volume_usd=20_000_000,
        max_spread_bps=15,
        min_listing_age_days=30,
        missing_listing_policy="allow",
        blocklist=["DOGE", "PEPE", "SHIB"],
        prepool_multiplier=3,
    )
    base.update(kwargs)
    return FilterConfig(**base)


def _ticker(base: str, **kwargs) -> Ticker:
    now = datetime.now(timezone.utc)
    values = dict(
        symbol=f"{base}/USDT",
        base=base,
        quote="USDT",
        last=100.0,
        bid=99.99,
        ask=100.01,
        quote_volume_24h=80_000_000,
        spot=True,
        active=True,
        listed_at=now - timedelta(days=400),
    )
    values.update(kwargs)
    return Ticker(**values)


def test_blocklist_spread_listing_and_top_n():
    now = datetime.now(timezone.utc)
    tickers = [
        _ticker("BTC", quote_volume_24h=90_000_000),
        _ticker("ETH", quote_volume_24h=80_000_000),
        _ticker("SOL", quote_volume_24h=70_000_000),
        _ticker("XRP", quote_volume_24h=60_000_000),
        _ticker("DOGE", quote_volume_24h=120_000_000),
        _ticker("PEPE", quote_volume_24h=110_000_000),
        _ticker("ETH3L", quote_volume_24h=100_000_000),
        _ticker("NEW", quote_volume_24h=95_000_000, listed_at=now - timedelta(days=2)),
        _ticker("WIDE", quote_volume_24h=94_000_000, bid=90, ask=110),
        _ticker("TINY", quote_volume_24h=1_000_000),
        _ticker("NOSPREAD", quote_volume_24h=93_000_000, bid=0, ask=0),
        _ticker("BTC", symbol="BTC/USD", quote="USD", quote_volume_24h=200_000_000),
        _ticker("PERP", symbol="BTC/USDT:USDT", quote_volume_24h=200_000_000),
        _ticker("DEAD", active=False, quote_volume_24h=200_000_000),
    ]
    outcome = filter_tokens(tickers, _cfg(prepool_multiplier=10), now=now)
    accepted = [row.ticker.symbol for row in outcome.accepted]
    assert accepted == ["BTC/USDT", "ETH/USDT", "SOL/USDT"]
    reasons = {row.ticker.symbol: row.reason for row in outcome.considered}
    assert reasons["DOGE/USDT"] == "blocklist"
    assert reasons["PEPE/USDT"] == "blocklist"
    assert reasons["ETH3L/USDT"] == "leveraged"
    assert reasons["NEW/USDT"] == "new_listing"
    assert reasons["WIDE/USDT"] == "spread"
    assert reasons["TINY/USDT"] == "min_volume"
    assert reasons["NOSPREAD/USDT"] == "missing_spread"
    assert reasons["XRP/USDT"] == "outside_top_n"
    assert "BTC/USD" not in reasons
    assert "BTC/USDT:USDT" not in reasons


def test_unknown_listing_policy():
    ticker = _ticker("ADA", listed_at=None)
    allowed = filter_tokens([ticker], _cfg(top_n=1), now=datetime.now(timezone.utc))
    assert allowed.accepted[0].ticker.symbol == "ADA/USDT"
    rejected = filter_tokens(
        [ticker],
        _cfg(top_n=1, missing_listing_policy="reject"),
        now=datetime.now(timezone.utc),
    )
    assert rejected.accepted == []
    assert rejected.considered[0].reason == "listing_unknown"


def test_leveraged_names_do_not_catch_jup():
    assert is_leveraged_base("ETH3L")
    assert is_leveraged_base("BTCUP")
    assert is_leveraged_base("ETHDOWN")
    assert not is_leveraged_base("JUP")
    assert not is_leveraged_base("BEAR")
    assert not is_leveraged_base("SUPER")


def test_spread_bps():
    assert spread_bps(100, 100) == pytest.approx(0)
    assert spread_bps(99, 101) == pytest.approx(200)
    assert spread_bps(0, 100) is None
    assert spread_bps(101, 99) is None


def test_volume_flag_and_forming_candle():
    spiked = make_candles(price=100, volume=100, spike_last=True, now_ms=10_000_000_000)
    ratio, _z, flagged = volume_signal(spiked, lookback=24, ratio_threshold=2, zscore_threshold=2)
    assert ratio == pytest.approx(6)
    assert flagged
    quiet = make_candles(price=100, volume=100, spike_last=False, now_ms=10_000_000_000)
    _ratio, _z, flagged = volume_signal(quiet, lookback=24, ratio_threshold=2, zscore_threshold=2)
    assert not flagged
    now_ms = 50_000_000
    tf = 3_600_000
    candles = [
        Candle(now_ms - tf, 1, 1, 1, 1, 1),
        Candle(now_ms, 1, 1, 1, 1, 9),
    ]
    closed = drop_forming(candles, now_ms=now_ms + 1_000, timeframe_ms=tf)
    assert [candle.ts for candle in closed] == [now_ms - tf]
