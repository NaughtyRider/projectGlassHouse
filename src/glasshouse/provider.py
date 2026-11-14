"""LLM provider interface and implementations.

Every provider returns a validated ``SQLGeneration``. Anything that cannot be turned into
one (bad JSON, wrong types, network failure) is raised as ``ProviderError``; the service
maps that to ``status: error``.
"""

from __future__ import annotations

import json
import re
import unicodedata
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, Mapping

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from glasshouse.config import DEFAULT_FIXTURE_PATH, Settings
from glasshouse.schema import PROMPT_SCHEMA


class SQLGeneration(BaseModel):
    """Structured output every provider must produce."""

    model_config = ConfigDict(extra="forbid", strict=True)

    sql: str | None = None
    tables_used: list[str] = Field(default_factory=list)
    refusal_reason: str | None = None

    @model_validator(mode="after")
    def _exactly_one_outcome(self) -> "SQLGeneration":
        has_sql = bool(self.sql and self.sql.strip())
        has_refusal = bool(self.refusal_reason and self.refusal_reason.strip())
        if has_sql == has_refusal:
            raise ValueError("exactly one of 'sql' or 'refusal_reason' must be set")
        return self


class ProviderError(Exception):
    """The provider failed or produced output that doesn't match SQLGeneration."""


SYSTEM_PROMPT = """\
You translate analysts' questions into a single read-only SQLite SELECT query for an
anti-money-laundering investigation tool.

Rules:
- Output exactly one SELECT statement (CTEs and UNION are fine). Never write, alter or
  inspect the schema (no INSERT/UPDATE/DELETE/DROP/PRAGMA/ATTACH, no sqlite_master).
- Use only the tables and columns listed in the schema.
- If the question cannot be answered from this schema, or asks for anything other than
  reading data, refuse by setting refusal_reason and leaving sql null.
- Respond with JSON only: {"sql": string|null, "tables_used": [string], "refusal_reason": string|null}

Schema:
"""


def build_prompt(question: str, schema: str = PROMPT_SCHEMA) -> str:
    """``schema`` is the role-filtered schema text; denied columns are never in it."""
    return f"{SYSTEM_PROMPT}{schema}\nQuestion: {question}\n"


def normalize_question(question: str) -> str:
    """Case-, whitespace-, punctuation- and unicode-insensitive key for fixture lookup."""
    q = unicodedata.normalize("NFKC", question).casefold()
    q = re.sub(r"\s+", " ", q).strip()
    return q.rstrip(" ?.!")


def parse_generation(payload: Any) -> SQLGeneration:
    """Validate raw provider output (dict or JSON string) into SQLGeneration."""
    try:
        if isinstance(payload, (str, bytes)):
            return SQLGeneration.model_validate_json(payload)
        return SQLGeneration.model_validate(payload)
    except ValidationError as e:
        first = e.errors()[0]
        loc = ".".join(str(p) for p in first.get("loc", ())) or "output"
        raise ProviderError(f"malformed provider output ({loc}: {first.get('msg')})") from None


class LLMProvider(ABC):
    name: str = "abstract"

    @abstractmethod
    def generate(self, question: str, schema: str = PROMPT_SCHEMA) -> SQLGeneration:
        """Translate ``question`` into SQL or a refusal using only ``schema`` (role-filtered).
        Raises ProviderError on bad output."""


class FakeProvider(LLMProvider):
    """Deterministic, offline provider backed by a fixture file keyed by normalised question."""

    name = "fake"
    UNKNOWN_REFUSAL = "I can only answer questions that are in the FakeProvider fixture set."

    def __init__(self, fixtures: Mapping[str, Any] | Path | str | None = None):
        if fixtures is None:
            fixtures = DEFAULT_FIXTURE_PATH
        if isinstance(fixtures, (str, Path)):
            data = json.loads(Path(fixtures).read_text(encoding="utf-8"))
            fixtures = data.get("responses", data)
        self._fixtures = {normalize_question(k): v for k, v in fixtures.items()}
        self.last_prompt: str | None = None

    def generate(self, question: str, schema: str = PROMPT_SCHEMA) -> SQLGeneration:
        self.last_prompt = build_prompt(question, schema)  # built for parity with real providers
        key = normalize_question(question)
        if key not in self._fixtures:
            return SQLGeneration(refusal_reason=self.UNKNOWN_REFUSAL)
        return parse_generation(self._fixtures[key])


class OllamaProvider(LLMProvider):
    """Optional local-model provider (``GLASSHOUSE_PROVIDER=ollama``). Never used by tests."""

    name = "ollama"

    def __init__(self, url: str, model: str, timeout: float = 60.0):
        self.url = url.rstrip("/")
        self.model = model
        self.timeout = timeout

    def generate(self, question: str, schema: str = PROMPT_SCHEMA) -> SQLGeneration:
        import httpx  # imported lazily so the package has no hard network dependency

        body = {
            "model": self.model,
            "stream": False,
            "format": SQLGeneration.model_json_schema(),
            "options": {"temperature": 0},
            "messages": [{"role": "user", "content": build_prompt(question, schema)}],
        }
        try:
            resp = httpx.post(f"{self.url}/api/chat", json=body, timeout=self.timeout)
            resp.raise_for_status()
            content = resp.json()["message"]["content"]
        except Exception as e:  # network, HTTP or shape errors
            raise ProviderError(f"ollama request failed: {e}") from None
        return parse_generation(content)


def get_provider(settings: Settings) -> LLMProvider:
    if settings.provider == "fake":
        return FakeProvider(settings.fixture_path)
    if settings.provider == "ollama":
        return OllamaProvider(settings.ollama_url, settings.ollama_model)
    raise ValueError(f"unknown provider: {settings.provider!r}")
