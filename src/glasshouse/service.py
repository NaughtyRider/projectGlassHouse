"""The /ask pipeline: provider -> validator -> guarded executor."""

from __future__ import annotations

import logging
import uuid
from typing import Any, Literal

from pydantic import BaseModel, Field

from glasshouse.config import Settings
from glasshouse.executor import ExecutionError, QueryTimeout, execute_query
from glasshouse.provider import LLMProvider, ProviderError
from glasshouse.validator import validate_sql

log = logging.getLogger("glasshouse")

Status = Literal["ok", "refused", "rejected", "error"]


class AskRequest(BaseModel):
    question: str = Field(min_length=1, max_length=2000)
    role: str = Field(min_length=1, max_length=64)


class AskResponse(BaseModel):
    request_id: str
    status: Status
    sql: str | None = None
    columns: list[str] = Field(default_factory=list)
    rows: list[list[Any]] = Field(default_factory=list)
    masked_columns: list[str] = Field(default_factory=list)
    reason: str | None = None


class AskService:
    def __init__(self, settings: Settings, provider: LLMProvider):
        self.settings = settings
        self.provider = provider

    def ask(self, req: AskRequest) -> AskResponse:
        request_id = uuid.uuid4().hex
        try:
            return self._ask(request_id, req)
        except Exception:  # last-resort guard: never surface a 500 to the analyst
            log.exception("unexpected failure in request %s", request_id)
            return AskResponse(request_id=request_id, status="error", reason="internal error")

    def _ask(self, request_id: str, req: AskRequest) -> AskResponse:
        # Task 1: role is accepted but not yet enforced.
        try:
            gen = self.provider.generate(req.question)
        except ProviderError as e:
            return AskResponse(request_id=request_id, status="error", reason=str(e))

        if gen.refusal_reason:
            return AskResponse(request_id=request_id, status="refused", reason=gen.refusal_reason)

        verdict = validate_sql(gen.sql, self.settings.allowed_tables)
        if not verdict.ok:
            return AskResponse(request_id=request_id, status="rejected", sql=gen.sql, reason=verdict.reason)

        try:
            result = execute_query(
                verdict.ast,
                self.settings.db_path,
                self.settings.allowed_tables,
                timeout_seconds=self.settings.timeout_seconds,
                default_limit=self.settings.default_limit,
                max_limit=self.settings.max_limit,
            )
        except QueryTimeout as e:
            return AskResponse(request_id=request_id, status="error", sql=gen.sql, reason=f"timeout: {e}")
        except ExecutionError as e:
            return AskResponse(request_id=request_id, status="error", sql=gen.sql, reason=str(e))

        return AskResponse(
            request_id=request_id,
            status="ok",
            sql=result.sql,
            columns=result.columns,
            rows=result.rows,
            masked_columns=[],
        )
