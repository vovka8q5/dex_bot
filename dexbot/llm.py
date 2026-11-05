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
    def __init__(self, message: str, *, status_code: int | None = None, body: object = None) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.body = body


def _chain(exc: BaseException):
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        yield current
        current = current.__cause__ or current.__context__


def _status_code(exc: BaseException) -> int | None:
    for item in _chain(exc):
        code = getattr(item, "status_code", None)
        if isinstance(code, int):
            return code
    return None


def _error_text(exc: BaseException) -> str:
    parts: list[str] = []
    for item in _chain(exc):
        parts.append(str(item))
        body = getattr(item, "body", None)
        if body is not None:
            parts.append(str(body))
    return " ".join(parts).lower()


def invalid_llm_credentials(exc: BaseException) -> bool:
    """True for a rejected key.

    xAI answers a bad key with HTTP 400 and ``Incorrect API key provided``.
    The SDK often leaves that sentence in the response body, so the exception
    text is only ``Error code: 400``. 401 and 403 are auth failures as well.
    A 400 about ``response_format`` is not.
    """
    status = _status_code(exc)
    text = _error_text(exc)
    if status in {401, 403}:
        return True
    phrases = (
        "incorrect api key",
        "invalid api key",
        "invalid_api_key",
        "api key provided",
        "unauthorized",
    )
    if any(phrase in text for phrase in phrases):
        return True
    if status == 400 and any(phrase in text for phrase in ("api key", "apikey", "authentication")):
        return True
    return False


def wrap_provider_error(exc: BaseException) -> ProviderError:
    if isinstance(exc, ProviderError):
        return exc
    return ProviderError(str(exc), status_code=_status_code(exc), body=getattr(exc, "body", None))


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


class UsageLog:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def add(self, **row: object) -> None:
        self.calls.append(row)


def read_token_usage(response: Any) -> tuple[int, int, int]:
    usage = getattr(response, "usage", None)
    if usage is None and isinstance(response, dict):
        usage = response.get("usage")
    if usage is None:
        return 0, 0, 0

    def grab(name: str, alt: str) -> int:
        if isinstance(usage, dict):
            value = usage.get(name)
            if value is None:
                value = usage.get(alt)
        else:
            value = getattr(usage, name, None)
            if value is None:
                value = getattr(usage, alt, None)
        return int(value or 0)

    prompt = grab("prompt_tokens", "input_tokens")
    completion = grab("completion_tokens", "output_tokens")
    total = grab("total_tokens", "total_tokens")
    return prompt, completion, total or (prompt + completion)


def estimate_cost(model: str, prompt: int, completion: int, prices: dict | None) -> float | None:
    if not prices or model not in prices:
        return None
    price = prices[model]
    input_price = price["input"] if isinstance(price, dict) else price.input
    output_price = price["output"] if isinstance(price, dict) else price.output
    return (prompt / 1_000_000) * float(input_price) + (completion / 1_000_000) * float(output_price)


class OpenAICompatibleLLM:
    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        timeout_s: float,
        temperature: float,
        client: Any = None,
        provider_name: str = "xai",
        prices: dict | None = None,
        usage: UsageLog | None = None,
    ) -> None:
        self.base_url = base_url
        self.temperature = temperature
        self.provider_name = provider_name
        self.prices = prices or {}
        self.usage = usage or UsageLog()
        self.last_citations: list[str] = []
        if client is not None:
            self.client = client
            return
        from openai import OpenAI

        self.client = OpenAI(base_url=base_url, api_key=api_key or "missing", timeout=timeout_s)

    def usage_calls(self) -> list[dict]:
        return list(self.usage.calls)

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
                    agent=agent,
                    system=system,
                    user=prompt,
                    search=search and attempt == 0,
                )
            except ProviderError:
                raise
            except Exception as exc:
                raise wrap_provider_error(exc) from exc
            try:
                parsed = parse_model(text, schema)
            except DecisionParseError as exc:
                last_error = exc
                continue
            self.last_citations = citations
            return parsed
        raise DecisionParseError(str(last_error))

    def _complete_text(self, *, model: str, agent: str, system: str, user: str, search: bool) -> tuple[str, list[str]]:
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
        prompt, completion, total = read_token_usage(response)
        self.usage.add(
            provider=self.provider_name,
            model=model,
            agent=agent,
            prompt_tokens=prompt,
            completion_tokens=completion,
            total_tokens=total,
            estimated_cost_usd=estimate_cost(model, prompt, completion, self.prices),
        )
        choice = response.choices[0].message
        content = choice.content or ""
        return content, _citations(response)

    def probe_auth(self) -> bool:
        """GET /models. True when the key is accepted.

        Raises ProviderError when the key is rejected. A transport failure
        returns False so auto mode can try the next provider.
        """
        models = getattr(self.client, "models", None)
        list_models = getattr(models, "list", None)
        if not callable(list_models):
            logger.warning("LLM auth probe unavailable for %s", self.provider_name)
            return False
        try:
            list_models()
        except ProviderError:
            raise
        except Exception as exc:
            wrapped = wrap_provider_error(exc)
            if invalid_llm_credentials(wrapped):
                raise wrapped from exc
            logger.warning(
                "LLM auth probe for %s failed without rejecting the key (%s)",
                self.provider_name,
                type(exc).__name__,
            )
            return False
        return True

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

    def __init__(
        self,
        settings: Settings,
        primary: Any = None,
        fallback: Any = None,
        *,
        provider_name: str = "xai",
        models: Any = None,
        live_search: bool = True,
    ) -> None:
        self.settings = settings
        self.provider_name = provider_name
        self.supports_live_search = live_search
        self._models = models
        self.last_citations: list[str] = []
        if primary is None:
            from dexbot.config import require_llm_key

            primary = OpenAICompatibleLLM(
                base_url=settings.llm.base_url,
                api_key=require_llm_key(settings),
                timeout_s=settings.llm.timeout_s,
                temperature=settings.llm.temperature,
                provider_name=provider_name,
                prices=settings.llm.prices_per_million,
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
                provider_name="fallback",
            )

    def model_for(self, agent: str) -> str:
        table = self._models if self._models is not None else self.settings.llm.models
        return getattr(table, agent)

    def usage_calls(self) -> list[dict]:
        reader = getattr(self.primary, "usage_calls", None)
        return list(reader()) if callable(reader) else []

    def complete_json(self, *, agent: str, symbol: str, schema: type, system: str, user: str, search: bool = False, model: str | None = None):
        if search and not self.supports_live_search:
            search = False
        chosen = model or self.model_for(agent)
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

    @property
    def provider_name(self) -> str:
        if self._rules_only:
            return "rules"
        return getattr(self.primary, "provider_name", "xai")

    @property
    def supports_live_search(self) -> bool:
        return (not self._rules_only) and bool(getattr(self.primary, "supports_live_search", False))

    def usage_calls(self) -> list[dict]:
        reader = getattr(self.primary, "usage_calls", None)
        return list(reader()) if callable(reader) else []

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
            _warn_rejected(self.env_name, exc)
            result = self.rules.complete_json(**kwargs)
            self.last_citations = []
            return result
        self.last_citations = list(getattr(self.primary, "last_citations", []))
        return result


def _warn_rejected(env_name: str, exc: BaseException) -> None:
    status = _status_code(exc)
    http = f"HTTP {status}" if status is not None else "auth error"
    logger.warning(
        "LLM fallback: %s was rejected (%s); agents are deterministic rules and news search is off",
        env_name,
        http,
    )


def _provider_order(settings: Settings) -> list[str]:
    choice = settings.llm.provider
    if choice == "auto":
        return ["xai", "deepseek"]
    return [choice]


def build_llm(settings: Settings, client: Any = None, deepseek_client: Any = None):
    """Pick xAI, DeepSeek, or the deterministic rules.

    ``auto`` probes xAI first, then DeepSeek. Each probe is GET /models.
    A rejected key (HTTP 400/401/403 auth) is skipped. The active provider
    name is logged; the key is not. Mid-run auth errors still switch to rules.
    """
    rules = RuleBasedLLM()
    for name in _provider_order(settings):
        if name == "deepseek":
            base_url = settings.llm.deepseek_base_url
            key_env = settings.llm.deepseek_api_key_env
            models = settings.llm.deepseek_models
            live_search = False
            if deepseek_client is not None:
                injected = deepseek_client
            elif settings.llm.provider == "deepseek":
                injected = client
            else:
                injected = None
        else:
            base_url = settings.llm.base_url
            key_env = settings.llm.api_key_env
            models = settings.llm.models
            live_search = True
            injected = client
        key = os.getenv(key_env, "").strip()
        if not key:
            logger.info("LLM provider %s skipped: %s is unset", name, key_env)
            continue
        chat = OpenAICompatibleLLM(
            base_url=base_url,
            api_key=key,
            timeout_s=settings.llm.timeout_s,
            temperature=settings.llm.temperature,
            client=injected,
            provider_name=name,
            prices=settings.llm.prices_per_million,
        )
        try:
            accepted = chat.probe_auth()
        except ProviderError as exc:
            if not invalid_llm_credentials(exc):
                raise
            _warn_rejected(key_env, exc)
            continue
        if not accepted:
            logger.info("LLM provider %s probe did not succeed", name)
            continue
        logger.info("LLM provider active: %s", name)
        routing = RoutingLLM(
            settings,
            primary=chat,
            provider_name=name,
            models=models,
            live_search=live_search,
        )
        return ResilientLLM(routing, rules, env_name=key_env)
    logger.warning("LLM fallback: no provider accepted a key; agents are deterministic rules and news search is off")
    return rules
