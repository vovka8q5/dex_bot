"""OpenAI-compatible chat client. xAI is the default; the base URL is config.

Live search uses xAI's ``search_parameters`` body (web + X). A fallback
OpenAI-compatible proxy is allowed only for the volume prefilter.
"""

from __future__ import annotations

import logging
import os
from typing import Any

from dexbot.config import PREFILTER_AGENTS, Settings
from dexbot.decisions import DecisionParseError, parse_model
from dexbot.rules import RuleBasedLLM

logger = logging.getLogger("dexbot.llm")


class ProviderError(RuntimeError):
    pass


def search_body() -> dict[str, Any]:
    """xAI live search over the web and X, sent via the OpenAI-compatible extra body."""
    return {
        "search_parameters": {
            "mode": "on",
            "return_citations": True,
            "max_search_results": 8,
            "sources": [{"type": "web"}, {"type": "x"}],
        }
    }


def _citations(response: Any) -> list[str]:
    raw = getattr(response, "citations", None)
    if raw is None:
        extra = getattr(response, "model_extra", None) or {}
        if isinstance(extra, dict):
            raw = extra.get("citations")
    if not raw:
        return []
    found: list[str] = []
    for item in raw:
        if isinstance(item, str):
            found.append(item)
        elif isinstance(item, dict) and item.get("url"):
            found.append(str(item["url"]))
    return found


class OpenAICompatibleLLM:
    def __init__(self, *, base_url: str, api_key: str, timeout_s: float, temperature: float, client: Any = None) -> None:
        self.base_url = base_url
        self.temperature = temperature
        self.last_citations: list[str] = []
        if client is not None:
            self.client = client
            return
        from openai import OpenAI

        self.client = OpenAI(base_url=base_url, api_key=api_key or "missing", timeout=timeout_s)

    def complete_json(
        self,
        *,
        agent: str,
        symbol: str,
        schema: type,
        system: str,
        user: str,
        search: bool = False,
        model: str | None = None,
    ):
        if not model:
            raise ProviderError("model is required")
        last_error: Exception | None = None
        for attempt in range(2):
            prompt = user
            if attempt and last_error is not None:
                prompt = (
                    user
                    + "\n\nThe previous reply failed validation: "
                    + str(last_error)
                    + "\nReply with one JSON object only."
                )
            try:
                text, citations = self._complete_text(
                    model=model,
                    system=system,
                    user=prompt,
                    search=search and attempt == 0,
                )
            except ProviderError:
                raise
            except Exception as exc:
                raise ProviderError(str(exc)) from exc
            try:
                parsed = parse_model(text, schema)
            except DecisionParseError as exc:
                last_error = exc
                continue
            self.last_citations = citations
            return parsed
        raise DecisionParseError(str(last_error))

    def _complete_text(self, *, model: str, system: str, user: str, search: bool) -> tuple[str, list[str]]:
        payload: dict[str, Any] = {
            "model": model,
            "temperature": self.temperature,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "response_format": {"type": "json_object"},
        }
        if search:
            payload["extra_body"] = search_body()
        response = self._create(payload)
        choice = response.choices[0].message
        content = choice.content or ""
        return content, _citations(response)

    def _create(self, payload: dict[str, Any]) -> Any:
        try:
            return self.client.chat.completions.create(**payload)
        except Exception as exc:
            message = str(exc).lower()
            retried = False
            if "response_format" in message and "response_format" in payload:
                payload = dict(payload)
                payload.pop("response_format", None)
                retried = True
            if "temperature" in message and "temperature" in payload:
                payload = dict(payload)
                payload.pop("temperature", None)
                retried = True
            if not retried:
                raise
            return self.client.chat.completions.create(**payload)


class RoutingLLM:
    """Primary provider, with an optional fallback limited to prefilter agents."""

    def __init__(self, settings: Settings, primary: Any = None, fallback: Any = None) -> None:
        self.settings = settings
        self.last_citations: list[str] = []
        if primary is None:
            from dexbot.config import require_llm_key

            primary = OpenAICompatibleLLM(
                base_url=settings.llm.base_url,
                api_key=require_llm_key(settings),
                timeout_s=settings.llm.timeout_s,
                temperature=settings.llm.temperature,
            )
        self.primary = primary
        self.fallback = fallback
        if fallback is None and settings.llm.fallback.enabled:
            key = os.getenv(settings.llm.fallback.api_key_env, "")
            self.fallback = OpenAICompatibleLLM(
                base_url=settings.llm.fallback.base_url,
                api_key=key or "not-needed",
                timeout_s=settings.llm.timeout_s,
                temperature=settings.llm.temperature,
            )

    def complete_json(self, *, agent: str, symbol: str, schema: type, system: str, user: str, search: bool = False, model: str | None = None):
        chosen = model or self.settings.llm.model_for(agent)
        try:
            result = self.primary.complete_json(
                agent=agent,
                symbol=symbol,
                schema=schema,
                system=system,
                user=user,
                search=search,
                model=chosen,
            )
            self.last_citations = list(getattr(self.primary, "last_citations", []))
            return result
        except ProviderError:
            if not self._can_fallback(agent):
                raise
            logger.warning("primary LLM failed for %s; using fallback prefilter", agent)
            assert self.fallback is not None
            result = self.fallback.complete_json(
                agent=agent,
                symbol=symbol,
                schema=schema,
                system=system,
                user=user,
                search=False,
                model=self.settings.llm.fallback.model or chosen,
            )
            self.last_citations = list(getattr(self.fallback, "last_citations", []))
            return result

    def _can_fallback(self, agent: str) -> bool:
        cfg = self.settings.llm.fallback
        return bool(
            self.fallback is not None
            and cfg.enabled
            and agent in PREFILTER_AGENTS
            and agent in set(cfg.agents)
        )


def invalid_llm_credentials(exc: BaseException) -> bool:
    text = str(exc).lower()
    return any(
        needle in text
        for needle in ("401", "invalid api key", "incorrect api key", "invalid_api_key", "unauthorized")
    )


class ResilientLLM:
    """Use the configured provider, and switch to rules if the key is rejected."""

    def __init__(self, primary: RoutingLLM, rules: RuleBasedLLM, env_name: str) -> None:
        self.primary = primary
        self.rules = rules
        self.env_name = env_name
        self._rules_only = False
        self.last_citations: list[str] = []

    @property
    def using_rules(self) -> bool:
        return self._rules_only

    def complete_json(self, **kwargs):
        if self._rules_only:
            result = self.rules.complete_json(**kwargs)
            self.last_citations = []
            return result
        try:
            result = self.primary.complete_json(**kwargs)
        except ProviderError as exc:
            if not invalid_llm_credentials(exc):
                raise
            self._rules_only = True
            logger.warning(
                "LLM fallback: %s was rejected; agents are deterministic rules and news search is off",
                self.env_name,
            )
            result = self.rules.complete_json(**kwargs)
            self.last_citations = []
            return result
        self.last_citations = list(getattr(self.primary, "last_citations", []))
        return result


def build_llm(settings: Settings):
    """Real provider when the key is present, otherwise deterministic rules."""
    key = os.getenv(settings.llm.api_key_env, "").strip()
    rules = RuleBasedLLM()
    if not key:
        logger.warning(
            "LLM fallback: %s is missing; agents are deterministic rules and news search is off",
            settings.llm.api_key_env,
        )
        return rules
    return ResilientLLM(RoutingLLM(settings), rules, env_name=settings.llm.api_key_env)
