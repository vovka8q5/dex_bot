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
                raise wrap_provider_error(exc) from exc
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

    def probe_auth(self) -> None:
        """GET /v1/models. Raise ProviderError when the key is rejected."""
        models = getattr(self.client, "models", None)
        list_models = getattr(models, "list", None)
        if not callable(list_models):
            logger.warning("LLM auth probe unavailable; chat calls will still be attempted")
            return
        try:
            list_models()
        except ProviderError:
            raise
        except Exception as exc:
            wrapped = wrap_provider_error(exc)
            if invalid_llm_credentials(wrapped):
                raise wrapped from exc
            logger.warning(
                "LLM auth probe failed without rejecting the key (%s); chat calls will still be attempted",
                type(exc).__name__,
            )

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


def build_llm(settings: Settings, client: Any = None):
    """Real provider when the key is accepted, otherwise deterministic rules.

    A present key is checked with GET /v1/models before the first symbol.
    HTTP 400/401/403 auth failures switch the whole run. The same switch
    happens mid-run if a later call rejects the key.
    """
    key = os.getenv(settings.llm.api_key_env, "").strip()
    rules = RuleBasedLLM()
    if not key:
        logger.warning(
            "LLM fallback: %s is missing; agents are deterministic rules and news search is off",
            settings.llm.api_key_env,
        )
        return rules
    chat = OpenAICompatibleLLM(
        base_url=settings.llm.base_url,
        api_key=key,
        timeout_s=settings.llm.timeout_s,
        temperature=settings.llm.temperature,
        client=client,
    )
    try:
        chat.probe_auth()
    except ProviderError as exc:
        if not invalid_llm_credentials(exc):
            raise
        _warn_rejected(settings.llm.api_key_env, exc)
        return rules
    return ResilientLLM(RoutingLLM(settings, primary=chat), rules, env_name=settings.llm.api_key_env)
