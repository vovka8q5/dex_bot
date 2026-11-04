"""Parse a team-lead decision out of model text."""

from __future__ import annotations

import json
import re
from typing import TypeVar

from pydantic import BaseModel, ValidationError

from dexbot.models import TeamDecision

T = TypeVar("T", bound=BaseModel)


class DecisionParseError(ValueError):
    pass


def extract_json_object(text: str) -> dict:
    """Take the first JSON object out of model text, fences included."""
    cleaned = text.strip()
    cleaned = re.sub(r"^```(?:json)?", "", cleaned, flags=re.IGNORECASE).strip()
    cleaned = re.sub(r"```$", "", cleaned).strip()
    start = cleaned.find("{")
    if start < 0:
        raise DecisionParseError("no JSON object")
    try:
        value, _end = json.JSONDecoder().raw_decode(cleaned[start:])
    except json.JSONDecodeError as exc:
        raise DecisionParseError(str(exc)) from exc
    if not isinstance(value, dict):
        raise DecisionParseError("JSON value is not an object")
    return value


def parse_model(text: str, schema: type[T]) -> T:
    try:
        return schema.model_validate(extract_json_object(text))
    except DecisionParseError:
        raise
    except ValidationError as exc:
        raise DecisionParseError(str(exc)) from exc


def parse_team_decision(text: str) -> TeamDecision:
    return parse_model(text, TeamDecision)
