"""Candle math used by the volume agent and the planner context.

The volume flag is computed here. Prompts can discuss it; they cannot set it.
"""

from __future__ import annotations

import statistics
import time
from datetime import datetime, timezone

from dexbot.config import VolumeConfig
from dexbot.models import Candle, MarketStats

TIMEFRAME_MS = {
    "1h": 3_600_000,
    "1m": 60_000,
    "4h": 14_400_000,
    "1d": 86_400_000,
}


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def drop_forming(candles: list[Candle], *, now_ms: int, timeframe_ms: int) -> list[Candle]:
    """Drop the candle that is still open. ccxt timestamps are open times."""
    if candles and candles[-1].ts + timeframe_ms > now_ms:
        return candles[:-1]
    return list(candles)


def make_candles(
    *,
    price: float,
    n: int = 48,
    volume: float = 100.0,
    spike_last: bool = False,
    now_ms: int | None = None,
    timeframe_ms: int = 3_600_000,
) -> list[Candle]:
    """Closed candles ending one full bar before ``now_ms``."""
    now_ms = int(time.time() * 1000) if now_ms is None else now_ms
    start_open = now_ms - n * timeframe_ms
    candles: list[Candle] = []
    for i in range(n):
        vol = volume * (6.0 if spike_last and i == n - 1 else 1.0)
        candles.append(
            Candle(
                ts=start_open + i * timeframe_ms,
                open=price,
                high=price * 1.01,
                low=price * 0.99,
                close=price,
                volume=vol,
            )
        )
    return candles


def rsi(closes: list[float], period: int = 14) -> float | None:
    if len(closes) < period + 1:
        return None
    gains: list[float] = []
    losses: list[float] = []
    for prev, cur in zip(closes, closes[1:]):
        delta = cur - prev
        gains.append(max(delta, 0.0))
        losses.append(max(-delta, 0.0))
    avg_gain = sum(gains[-period:]) / period
    avg_loss = sum(losses[-period:]) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


def atr(candles: list[Candle], period: int = 14) -> float | None:
    if len(candles) < period + 1:
        return None
    true_ranges: list[float] = []
    for prev, cur in zip(candles, candles[1:]):
        true_ranges.append(
            max(cur.high - cur.low, abs(cur.high - prev.close), abs(cur.low - prev.close))
        )
    return sum(true_ranges[-period:]) / period


def sma(closes: list[float], period: int = 20) -> float | None:
    if len(closes) < period:
        return None
    return sum(closes[-period:]) / period


def volume_signal(
    candles: list[Candle],
    *,
    lookback: int,
    ratio_threshold: float,
    zscore_threshold: float,
) -> tuple[float | None, float | None, bool]:
    """Compare the last closed bar with the previous ``lookback`` bars.

    Returns ``(ratio, zscore, flagged)``. Flagged when the ratio or the z-score
    clears its threshold. A flat zero-volume window is not a flag.
    """
    if len(candles) < lookback + 1:
        return None, None, False
    window = [candle.volume for candle in candles[-(lookback + 1) : -1]]
    last = candles[-1].volume
    median = statistics.median(window)
    ratio = (last / median) if median > 0 else None
    mean = statistics.fmean(window)
    stdev = statistics.pstdev(window)
    zscore = ((last - mean) / stdev) if stdev > 0 else 0.0
    flagged = bool((ratio is not None and ratio >= ratio_threshold) or zscore >= zscore_threshold)
    return ratio, zscore, flagged


def compute_stats(candles: list[Candle], volume_cfg: VolumeConfig) -> MarketStats:
    ordered = sorted(candles, key=lambda candle: candle.ts)
    closes = [candle.close for candle in ordered]
    ratio, zscore, flagged = volume_signal(
        ordered,
        lookback=volume_cfg.lookback,
        ratio_threshold=volume_cfg.ratio_threshold,
        zscore_threshold=volume_cfg.zscore_threshold,
    )
    last = closes[-1] if closes else 0.0
    ret_24h = None
    if len(closes) >= 25 and closes[-25] > 0:
        ret_24h = closes[-1] / closes[-25] - 1.0
    median = None
    last_volume = None
    if len(ordered) >= volume_cfg.lookback + 1:
        last_volume = ordered[-1].volume
        median = statistics.median(
            candle.volume for candle in ordered[-(volume_cfg.lookback + 1) : -1]
        )
    return MarketStats(
        last=last,
        ret_24h=ret_24h,
        rsi_14=rsi(closes) if closes else None,
        atr_14=atr(ordered) if ordered else None,
        sma_20=sma(closes) if closes else None,
        volume_ratio=ratio,
        volume_zscore=zscore,
        last_volume=last_volume,
        median_volume=median,
        candle_count=len(ordered),
        volume_flagged=flagged,
    )
