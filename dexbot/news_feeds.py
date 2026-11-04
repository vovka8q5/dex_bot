"""Keyless RSS headlines for the news agent when the provider has no live search.

One cache is filled per cycle. A feed that fails is skipped.
"""

from __future__ import annotations

import logging
import re
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

from dexbot.config import NewsConfig

logger = logging.getLogger("dexbot.news")

_NAMES = {
    "BTC": "bitcoin",
    "ETH": "ethereum",
    "SOL": "solana",
    "XRP": "ripple",
    "DOGE": "dogecoin",
    "ADA": "cardano",
    "AVAX": "avalanche",
    "DOT": "polkadot",
    "LINK": "chainlink",
    "LTC": "litecoin",
    "BCH": "bitcoin cash",
    "TRX": "tron",
    "TON": "toncoin",
    "NEAR": "near protocol",
    "APT": "aptos",
    "SUI": "sui",
    "ATOM": "cosmos",
    "UNI": "uniswap",
    "AAVE": "aave",
    "BNB": "bnb",
}


@dataclass(frozen=True)
class Headline:
    title: str
    link: str
    source: str
    published: datetime | None
    summary: str


def urllib_fetcher(url: str, timeout: float) -> str:
    request = urllib.request.Request(url, headers={"User-Agent": "dexbot/0.1 (+rss)"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read().decode("utf-8", errors="replace")


def parse_time(value: str | None) -> datetime | None:
    if not value or not value.strip():
        return None
    text = value.strip()
    try:
        parsed = parsedate_to_datetime(text)
    except (TypeError, ValueError, IndexError, OverflowError):
        parsed = None
    if parsed is None:
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _text(node: ET.Element | None) -> str:
    if node is None or node.text is None:
        return ""
    raw = re.sub(r"<[^>]+>", " ", node.text)
    return re.sub(r"\s+", " ", raw).strip()


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def parse_feed(xml_text: str, source: str) -> list[Headline]:
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        logger.warning("rss feed %s was not valid xml", source)
        return []
    items: list[Headline] = []
    for node in root.iter():
        if _local(node.tag) not in {"item", "entry"}:
            continue
        title = link = summary = ""
        published = None
        for child in list(node):
            name = _local(child.tag)
            if name == "title" and not title:
                title = _text(child)
            elif name in {"description", "summary", "content"} and not summary:
                summary = _text(child)[:240]
            elif name == "pubDate" or name in {"published", "updated", "date"}:
                published = published or parse_time(child.text)
            elif name == "link" and not link:
                href = child.attrib.get("href")
                link = (href or _text(child) or (child.text or "")).strip()
        if title:
            items.append(Headline(title=title, link=link, source=source, published=published, summary=summary))
    return items


def needles_for(symbol: str) -> list[str]:
    base = symbol.split("/")[0].upper()
    found = [base.lower()]
    name = _NAMES.get(base)
    if name and name not in found:
        found.append(name)
    return found


def mentions(text: str, needles: list[str]) -> bool:
    hay = text.lower()
    for needle in needles:
        if re.search(rf"(?<![a-z0-9]){re.escape(needle)}(?![a-z0-9])", hay):
            return True
    return False


class HeadlineCache:
    """Fetch every configured feed once, then filter per symbol."""

    def __init__(self, cfg: NewsConfig, *, fetcher=None, now: datetime | None = None) -> None:
        self.cfg = cfg
        self.fetcher = fetcher or urllib_fetcher
        self.now = now or datetime.now(timezone.utc)
        self._items: list[Headline] | None = None

    def load(self) -> list[Headline]:
        if self._items is not None:
            return self._items
        collected: list[Headline] = []
        for feed in self.cfg.feeds:
            try:
                payload = self.fetcher(feed.url, self.cfg.timeout_s)
                collected.extend(parse_feed(payload, feed.name))
            except Exception:
                logger.warning("rss feed failed: %s", feed.name)
        self._items = collected
        return collected

    def for_symbol(self, symbol: str, *, limit: int = 8) -> list[dict]:
        if self.now.tzinfo is None:
            moment = self.now.replace(tzinfo=timezone.utc)
        else:
            moment = self.now.astimezone(timezone.utc)
        cutoff = moment - timedelta(hours=self.cfg.lookback_hours)
        needles = needles_for(symbol)
        matched: list[Headline] = []
        for item in self.load():
            if item.published is None or item.published < cutoff:
                continue
            blob = f"{item.title} {item.summary}"
            if mentions(blob, needles):
                matched.append(item)
        matched.sort(key=lambda item: item.published or cutoff, reverse=True)
        return [
            {
                "title": item.title,
                "link": item.link,
                "source": item.source,
                "published": item.published.isoformat() if item.published else None,
                "summary": item.summary,
            }
            for item in matched[:limit]
        ]
