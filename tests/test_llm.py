import json
from types import SimpleNamespace

import pytest

from dexbot.config import FallbackConfig, LLMConfig, Settings
from dexbot.decisions import DecisionParseError
from dexbot.llm import OpenAICompatibleLLM, ProviderError, RoutingLLM, search_body
from dexbot.models import NewsReport, VolumeVerdict


class _Completions:
    def __init__(self, contents: list[str]) -> None:
        self.contents = contents
        self.calls: list[dict] = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        content = self.contents[min(len(self.calls) - 1, len(self.contents) - 1)]
        if content == "RAISE_FORMAT":
            raise RuntimeError("unsupported response_format")
        message = SimpleNamespace(content=content)
        return SimpleNamespace(choices=[SimpleNamespace(message=message)], citations=["https://x.com/example"])


def _client(contents: list[str]) -> SimpleNamespace:
    completions = _Completions(contents)
    return SimpleNamespace(chat=SimpleNamespace(completions=completions), completions=completions)


def _volume_json() -> str:
    return json.dumps(
        {
            "symbol": "BTC/USDT",
            "flagged": True,
            "volume_ratio": 3,
            "zscore": 2.5,
            "stance": "buy",
            "reasoning": "participation expanded",
        }
    )


def test_search_body_asks_for_web_and_x():
    body = search_body()
    kinds = {source["type"] for source in body["search_parameters"]["sources"]}
    assert kinds == {"web", "x"}
    assert body["search_parameters"]["mode"] == "on"


def test_client_sends_search_and_retries_invalid_json():
    client = _client(["not json", _volume_json()])
    llm = OpenAICompatibleLLM(base_url="https://api.x.ai/v1", api_key="test", timeout_s=5, temperature=0, client=client)
    parsed = llm.complete_json(
        agent="volume",
        symbol="BTC/USDT",
        schema=VolumeVerdict,
        system="sys",
        user="user",
        search=True,
        model="grok-4-fast",
    )
    assert parsed.flagged is True
    assert llm.last_citations == ["https://x.com/example"]
    extra = client.completions.calls[0]["extra_body"]["search_parameters"]
    assert {item["type"] for item in extra["sources"]} == {"web", "x"}
    assert "response_format" in client.completions.calls[0]
    assert len(client.completions.calls) == 2


def test_drops_response_format_when_the_proxy_rejects_it(monkeypatch):
    client = _client(["RAISE_FORMAT", _volume_json()])

    def create(**kwargs):
        client.completions.calls.append(kwargs)
        if "response_format" in kwargs:
            raise RuntimeError("response_format is not supported")
        message = SimpleNamespace(content=_volume_json())
        return SimpleNamespace(choices=[SimpleNamespace(message=message)], citations=[])

    client.chat.completions.create = create
    llm = OpenAICompatibleLLM(base_url="http://proxy/v1", api_key="test", timeout_s=5, temperature=0.2, client=client)
    parsed = llm.complete_json(
        agent="volume",
        symbol="BTC/USDT",
        schema=VolumeVerdict,
        system="sys",
        user="user",
        search=False,
        model="m",
    )
    assert isinstance(parsed, VolumeVerdict)
    assert "response_format" not in client.completions.calls[-1]


def test_invalid_json_twice_raises():
    client = _client(["nope", "still nope"])
    llm = OpenAICompatibleLLM(base_url="https://api.x.ai/v1", api_key="test", timeout_s=5, temperature=0, client=client)
    with pytest.raises(DecisionParseError):
        llm.complete_json(
            agent="news",
            symbol="BTC/USDT",
            schema=NewsReport,
            system="sys",
            user="user",
            search=False,
            model="grok-4",
        )


class _Down:
    last_citations: list[str] = []

    def complete_json(self, **kwargs):
        raise ProviderError("primary down")


class _Fallback:
    def __init__(self) -> None:
        self.last_citations: list[str] = []
        self.kwargs = None

    def complete_json(self, **kwargs):
        self.kwargs = kwargs
        return VolumeVerdict(
            symbol=kwargs["symbol"],
            flagged=False,
            volume_ratio=1,
            zscore=0,
            stance="wait",
            reasoning="fallback prefilter",
        )


def test_fallback_is_only_the_volume_prefilter():
    settings = Settings(
        llm=LLMConfig(fallback=FallbackConfig(enabled=True, agents=["volume"], model="cheap"))
    )
    fallback = _Fallback()
    router = RoutingLLM(settings, primary=_Down(), fallback=fallback)
    verdict = router.complete_json(
        agent="volume",
        symbol="ETH/USDT",
        schema=VolumeVerdict,
        system="s",
        user="u",
        search=True,
        model=None,
    )
    assert verdict.reasoning == "fallback prefilter"
    assert fallback.kwargs["search"] is False
    assert fallback.kwargs["model"] == "cheap"
    with pytest.raises(ProviderError):
        router.complete_json(
            agent="news",
            symbol="ETH/USDT",
            schema=NewsReport,
            system="s",
            user="u",
            search=True,
            model="grok-4",
        )
