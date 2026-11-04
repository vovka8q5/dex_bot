"""Spot USDT universe filter. No LLM calls."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from dexbot.config import FilterConfig
from dexbot.models import Candidate, Ticker

# Configurable default. Stablecoin bases are listed so the book does not
# treat USDC/USDT as a trade. Memecoin bases are listed by ticker, not by a
# fuzzy name match, so a token is excluded only when an operator put it here.
DEFAULT_BLOCKLIST: tuple[str, ...] = (
    "DOGE",
    "SHIB",
    "PEPE",
    "FLOKI",
    "BONK",
    "WIF",
    "MEME",
    "BOME",
    "MEW",
    "TURBO",
    "NEIRO",
    "PEOPLE",
    "LADYS",
    "BABYDOGE",
    "ELON",
    "SAMO",
    "MYRO",
    "POPCAT",
    "MOG",
    "BRETT",
    "PNUT",
    "ACT",
    "GOAT",
    "FARTCOIN",
    "TRUMP",
    "MELANIA",
    "AIDOGE",
    "WOJAK",
    "TOSHI",
    "DEGEN",
    "MOODENG",
    "DOGS",
    "BODEN",
    "SLERF",
    "PONKE",
    "GIGA",
    "MICHI",
    "USDC",
    "FDUSD",
    "TUSD",
    "DAI",
    "USDE",
    "BUSD",
    "PYUSD",
    "EUR",
)


def is_leveraged_base(base: str) -> bool:
    """Leveraged token suffixes. ``JUP`` is not treated as an UP token."""
    name = base.upper()
    for suffix in ("3L", "3S", "5L", "5S"):
        if name.endswith(suffix) and len(name) > len(suffix):
            return True
    for suffix in ("UP", "DOWN", "BULL", "BEAR"):
        # Require a real prefix so JUP / BEAR themselves are left to the blocklist.
        if name.endswith(suffix) and len(name) > len(suffix) + 2:
            return True
    return False


def spread_bps(bid: float | None, ask: float | None) -> float | None:
    if bid is None or ask is None or bid <= 0 or ask <= 0 or ask < bid:
        return None
    mid = (ask + bid) / 2.0
    if mid <= 0:
        return None
    return (ask - bid) / mid * 10_000.0


def listing_reject(
    listed_at: datetime | None,
    *,
    now: datetime,
    min_age_days: int,
    missing_policy: str,
) -> str | None:
    if listed_at is None:
        return "listing_unknown" if missing_policy == "reject" else None
    if listed_at.tzinfo is None:
        listed_at = listed_at.replace(tzinfo=timezone.utc)
    age = now - listed_at.astimezone(timezone.utc)
    if age < timedelta(days=min_age_days):
        return "new_listing"
    return None


def _reject(
    ticker: Ticker,
    cfg: FilterConfig,
    block: set[str],
    now: datetime,
) -> tuple[str | None, float | None]:
    spread = spread_bps(ticker.bid, ticker.ask)
    if is_leveraged_base(ticker.base):
        return "leveraged", spread
    if ticker.base.upper() in block:
        return "blocklist", spread
    listing = listing_reject(
        ticker.listed_at,
        now=now,
        min_age_days=cfg.min_listing_age_days,
        missing_policy=cfg.missing_listing_policy,
    )
    if listing:
        return listing, spread
    if spread is None:
        return "missing_spread", None
    if spread > cfg.max_spread_bps:
        return "spread", spread
    if ticker.quote_volume_24h < cfg.min_quote_volume_usd:
        return "min_volume", spread
    return None, spread


@dataclass
class FilterOutcome:
    accepted: list[Candidate]
    considered: list[Candidate]


def filter_tokens(
    tickers: list[Ticker],
    cfg: FilterConfig,
    *,
    now: datetime | None = None,
) -> FilterOutcome:
    """Rank spot quote pairs and keep the top of the book that clears checks.

    Pairs outside the volume pre-pool are not candidates. There is no padding
    when fewer than ``top_n`` names survive.
    """
    moment = now or datetime.now(timezone.utc)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    block = {item.upper() for item in cfg.blocklist}
    universe = [
        ticker
        for ticker in tickers
        if ticker.spot
        and ticker.active
        and ticker.quote.upper() == cfg.quote.upper()
        and ":" not in ticker.symbol
        and ticker.last is not None
        and ticker.last > 0
    ]
    universe.sort(key=lambda ticker: (-ticker.quote_volume_24h, ticker.symbol))
    pre_n = max(cfg.top_n * max(cfg.prepool_multiplier, 1), cfg.top_n)
    prepool = universe[:pre_n]

    eligible: list[Ticker] = []
    considered: list[Candidate] = []
    for ticker in prepool:
        reason, spread = _reject(ticker, cfg, block, moment)
        if reason is None:
            eligible.append(ticker)
            considered.append(Candidate(ticker, spread, True, None))
        else:
            considered.append(Candidate(ticker, spread, False, reason))

    chosen = {id(ticker) for ticker in eligible[: cfg.top_n]}
    accepted: list[Candidate] = []
    final: list[Candidate] = []
    for row in considered:
        if row.passed and id(row.ticker) not in chosen:
            row = Candidate(row.ticker, row.spread_bps, False, "outside_top_n")
        final.append(row)
        if row.passed:
            accepted.append(row)
    return FilterOutcome(accepted=accepted, considered=final)
