import json
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from dexbot.config import LLMConfig, Settings
from dexbot.journal import Journal
from dexbot.llm import build_llm
from dexbot.news_feeds import HeadlineCache, parse_feed
from dexbot.report import load_report, render_report

_RSS = """<?xml version="1.0"?>
<rss version="2.0">
  <channel>
    <item>
      <title>Bitcoin ETF inflows jump</title>
      <link>https://example.com/btc</link>
      <pubDate>Tue, 03 Nov 2026 12:00:00 GMT</pubDate>
      <description>Spot bitcoin demand</description>
    </item>
    <item>
      <title>Ethereum upgrade ships</title>
      <link>https://example.com/eth</link>
      <pubDate>Tue, 03 Nov 2026 12:00:00 GMT</pubDate>
      <description>Validators ready</description>
    </item>
    <item>
      <title>Old bitcoin story</title>
      <link>https://example.com/old</link>
      <pubDate>Mon, 01 Sep 2026 12:00:00 GMT</pubDate>
      <description>Ancient bitcoin note</description>
    </item>
  </channel>
</rss>
"""


def _json_response(payload: dict):
    message = SimpleNamespace(content=json.dumps(payload))
    usage = SimpleNamespace(prompt_tokens=1000, completion_tokens=500, total_tokens=1500)
    return SimpleNamespace(choices=[SimpleNamespace(message=message)], usage=usage, citations=[])


class _Models:
    def __init__(self, error=None) -> None:
        self.error = error
        self.calls = 0

    def list(self):
        self.calls += 1
        if self.error is not None:
            raise self.error
        return []


class _Chat:
    def __init__(self, response=None, error=None) -> None:
        self.response = response
        self.error = error
        self.calls: list[dict] = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        return self.response or _usage_response()


def _endpoint(models_error=None, chat_error=None, response=None):
    chat = _Chat(response=response or _json_response({"ok": True}), error=chat_error)
    client = SimpleNamespace(models=_Models(models_error), chat=SimpleNamespace(completions=chat))
    return client, chat


def _auth_400():
    exc = Exception("Error code: 400")
    exc.status_code = 400
    exc.body = {"error": "Incorrect API key provided"}
    return exc


def test_rss_filters_recent_mentions_and_skips_a_broken_feed():
    def fetcher(url, timeout):
        if "broken" in url:
            raise OSError("down")
        return _RSS

    cache = HeadlineCache(Settings().news, fetcher=fetcher, now=datetime(2026, 11, 4, tzinfo=timezone.utc))
    cache.cfg.feeds = [
        type(cache.cfg.feeds[0])(name="broken", url="https://example.com/broken"),
        type(cache.cfg.feeds[0])(name="desk", url="https://example.com/rss"),
    ]
    btc = cache.for_symbol("BTC/USDT")
    eth = cache.for_symbol("ETH/USDT")
    assert [item["title"] for item in btc] == ["Bitcoin ETF inflows jump"]
    assert btc[0]["link"] == "https://example.com/btc"
    assert eth[0]["title"] == "Ethereum upgrade ships"
    assert cache._items is not None
    again = cache.for_symbol("BTC/USDT")
    assert again == btc
    assert parse_feed("<not xml", "x") == []


def test_auto_uses_deepseek_when_xai_key_is_rejected(monkeypatch, caplog):
    monkeypatch.setenv("XAI_API_KEY", "xai-secret")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "deepseek-secret")
    caplog.set_level("INFO")
    xai, _xai_chat = _endpoint(models_error=_auth_400())
    deep, deep_chat = _endpoint(
        response=_json_response(
            {
                "symbol": "BTC/USDT",
                "flagged": False,
                "volume_ratio": 1,
                "zscore": 0,
                "stance": "wait",
                "reasoning": "quiet tape",
            }
        )
    )
    settings = Settings(llm=LLMConfig(provider="auto"))
    llm = build_llm(settings, client=xai, deepseek_client=deep)
    assert llm.provider_name == "deepseek"
    assert llm.supports_live_search is False
    assert "LLM provider active: deepseek" in caplog.text
    assert "xai-secret" not in caplog.text
    assert "deepseek-secret" not in caplog.text
    from dexbot.models import VolumeVerdict

    llm.complete_json(
        agent="volume",
        symbol="BTC/USDT",
        schema=VolumeVerdict,
        system="sys",
        user=json.dumps(
            {
                "symbol": "BTC/USDT",
                "flagged": False,
                "volume_ratio": 1,
                "zscore": 0,
                "stance": "wait",
                "reasoning": "ok",
            }
        ),
        search=True,
        model=None,
    )
    sent = deep_chat.calls[0]
    assert sent["model"] == "deepseek-chat"
    assert sent["response_format"] == {"type": "json_object"}
    assert "extra_body" not in sent
    usage = llm.usage_calls()
    assert usage[0]["prompt_tokens"] == 1000
    assert usage[0]["completion_tokens"] == 500
    assert usage[0]["estimated_cost_usd"] == pytest.approx(1000 / 1_000_000 * 0.27 + 500 / 1_000_000 * 1.10)


def test_auto_falls_back_to_rules_when_both_keys_fail(monkeypatch):
    monkeypatch.setenv("XAI_API_KEY", "xai-secret")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "deepseek-secret")
    xai, _ = _endpoint(models_error=_auth_400())
    deep, _ = _endpoint(models_error=_auth_400())
    llm = build_llm(Settings(llm=LLMConfig(provider="auto")), client=xai, deepseek_client=deep)
    assert llm.provider_name == "rules"
    assert llm.using_rules is True


def test_explicit_deepseek_does_not_probe_xai(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "deepseek-secret")
    monkeypatch.delenv("XAI_API_KEY", raising=False)
    deep, deep_chat = _endpoint(
        response=_json_response(
            {
                "symbol": "BTC/USDT",
                "items": [],
                "overall": "none",
                "stance": "wait",
                "reasoning": "no fresh headlines",
            }
        )
    )
    llm = build_llm(Settings(llm=LLMConfig(provider="deepseek")), client=deep)
    assert llm.provider_name == "deepseek"
    assert deep.models.calls == 1
    from dexbot.models import NewsReport

    parsed = llm.complete_json(
        agent="news",
        symbol="BTC/USDT",
        schema=NewsReport,
        system="sys",
        user=json.dumps(
            {
                "symbol": "BTC/USDT",
                "items": [],
                "overall": "none",
                "stance": "wait",
                "reasoning": "no fresh headlines",
            }
        ),
        search=True,
        model=None,
    )
    assert parsed.overall == "none"
    assert deep_chat.calls[0]["model"] == "deepseek-chat"
    assert "extra_body" not in deep_chat.calls[0]


def test_report_shows_token_cost(tmp_path):
    journal = Journal(tmp_path / "journal.sqlite")
    now = datetime(2026, 11, 4, tzinfo=timezone.utc)
    run_id = journal.start_run(mode="demo", exchange="bybit-demo", mock=False, started_at=now)
    journal.record_llm_usage(
        run_id,
        [
            {
                "provider": "deepseek",
                "model": "deepseek-chat",
                "agent": "news",
                "prompt_tokens": 1000,
                "completion_tokens": 500,
                "total_tokens": 1500,
                "estimated_cost_usd": 0.00082,
            }
        ],
        created_at=now,
    )
    journal.finish_run(run_id, "ok", finished_at=now)
    text = render_report(load_report(Settings(), journal))
    journal.close()
    assert "LLM Последний прогон #1 (deepseek)" in text
    assert "токены 1500" in text
    assert "prompt 1000" in text
    assert "$0.000820" in text
