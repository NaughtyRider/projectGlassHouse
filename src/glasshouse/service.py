"""The /ask pipeline.

    role check -> role-filtered prompt -> provider -> validator -> access policy (AST)
               -> guarded executor (authorizer also enforces denied columns) -> lineage masking
    ... and exactly one audit record per request, written in one place (``ask``).
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel, Field

from glasshouse.access import check_access
from glasshouse.audit import AuditError, AuditLog, AuditRecord
from glasshouse.config import Settings
from glasshouse.executor import ExecutionError, QueryTimeout, execute_query
from glasshouse.masking import apply_mask_plan
from glasshouse.policy import AccessPolicy, UnknownRoleError
from glasshouse.provider import LLMProvider, ProviderError
from glasshouse.validator import validate_sql

log = logging.getLogger("glasshouse")

Status = Literal["ok", "refused", "rejected", "error"]


class AskRequest(BaseModel):
    question: str = Field(min_length=1, max_length=2000)
    # Deliberately loose: a missing / wrong-typed role must produce 403 from the policy layer,
    # not a 422 from request validation, and must never be defaulted.
    role: Any = None


class AskResponse(BaseModel):
    request_id: str
    status: Status
    sql: str | None = None
    columns: list[str] = Field(default_factory=list)
    rows: list[list[Any]] = Field(default_factory=list)
    masked_columns: list[str] = Field(default_factory=list)
    reason: str | None = None


@dataclass
class AskOutcome:
    response: AskResponse
    http_status: int = 200
    generated_sql: str | None = None  # raw provider SQL, for the audit trail


class AskService:
    def __init__(self, settings: Settings, provider: LLMProvider, policy: AccessPolicy, audit: AuditLog):
        self.settings = settings
        self.provider = provider
        self.policy = policy
        self.audit = audit

    def ask(self, req: AskRequest) -> AskOutcome:
        request_id = uuid.uuid4().hex
        try:
            outcome = self._process(request_id, req)
        except Exception:  # last-resort guard: never surface a 500 to the analyst
            log.exception("unexpected failure in request %s", request_id)
            outcome = AskOutcome(AskResponse(request_id=request_id, status="error", reason="internal error"))

        if not self._audit(outcome, req.role, req.question):
            # Fail closed: no data leaves the system without an audit trail.
            return AskOutcome(
                AskResponse(request_id=request_id, status="error", reason="audit log unavailable; request not served"),
                http_status=outcome.http_status,
                generated_sql=outcome.generated_sql,
            )
        return outcome

    def reject_invalid_request(self, raw_body: Any, errors: list[dict]) -> AskOutcome:
        """Handle a POST /ask body that failed FastAPI validation (missing fields, bad JSON, ...).

        The role is checked first, so a missing/unknown role is always 403, even when other
        fields are also invalid. Otherwise the caller returns FastAPI's standard 422. Either way
        exactly one audit record is written; the provider and ledger are never touched.
        """
        request_id = uuid.uuid4().hex
        payload = raw_body if isinstance(raw_body, dict) else {}
        role, question = payload.get("role"), payload.get("question")
        try:
            self.policy.get_role(role)
        except UnknownRoleError as e:
            outcome = AskOutcome(AskResponse(request_id=request_id, status="rejected", reason=str(e)), http_status=403)
        else:
            fields = sorted({".".join(str(p) for p in err.get("loc", ())[1:]) or "body" for err in errors})
            outcome = AskOutcome(
                AskResponse(request_id=request_id, status="rejected",
                            reason="invalid request: " + ", ".join(f"{f} invalid" for f in fields)),
                http_status=422,
            )
        self._audit(outcome, role, question)  # nothing is served either way, so no fail-closed swap needed
        return outcome

    def _audit(self, outcome: AskOutcome, role: Any, question: Any) -> bool:
        """Write the one audit record for this request. Returns False if it could not be written."""
        resp = outcome.response
        try:
            self.audit.record(AuditRecord(
                request_id=resp.request_id,
                role=role if isinstance(role, str) else (None if role is None else repr(role)),
                question=question if isinstance(question, str) else (None if question is None else repr(question)),
                generated_sql=outcome.generated_sql,
                final_status=resp.status,
                reason=resp.reason,
                row_count=len(resp.rows),
                masked_columns=resp.masked_columns,
            ))
        except AuditError:
            log.exception("audit write failed for request %s", resp.request_id)
            return False
        return True

    def _process(self, rid: str, req: AskRequest) -> AskOutcome:
        # 1. Role: exact match against the policy, never defaulted.
        try:
            role = self.policy.get_role(req.role)
        except UnknownRoleError as e:
            return AskOutcome(AskResponse(request_id=rid, status="rejected", reason=str(e)), http_status=403)

        # 2. Provider sees only the schema this role is allowed to see.
        try:
            gen = self.provider.generate(req.question, role.prompt_schema())
        except ProviderError as e:
            return AskOutcome(AskResponse(request_id=rid, status="error", reason=str(e)))

        if gen.refusal_reason:
            return AskOutcome(AskResponse(request_id=rid, status="refused", reason=gen.refusal_reason))
        sql = gen.sql

        def rejected(reason: str) -> AskOutcome:
            return AskOutcome(AskResponse(request_id=rid, status="rejected", sql=sql, reason=reason), generated_sql=sql)

        def error(reason: str) -> AskOutcome:
            return AskOutcome(AskResponse(request_id=rid, status="error", sql=sql, reason=reason), generated_sql=sql)

        # 3. Structural safety (Task 1).
        verdict = validate_sql(sql, self.settings.allowed_tables)
        if not verdict.ok:
            return rejected(verdict.reason)

        # 4. Column-level policy + output lineage on the AST.
        decision = check_access(verdict.ast, role)
        if not decision.ok:
            return rejected(f"access policy: {decision.reason}")

        # 5. Guarded execution. The authorizer re-checks denied columns inside SQLite.
        try:
            result = execute_query(
                verdict.ast,
                self.settings.db_path,
                self.settings.allowed_tables,
                timeout_seconds=self.settings.timeout_seconds,
                default_limit=self.settings.default_limit,
                max_limit=self.settings.max_limit,
                denied_columns=role.denied_columns(),
            )
        except QueryTimeout as e:
            return error(f"timeout: {e}")
        except ExecutionError as e:
            return error(str(e))

        # 6. Mask by output position according to lineage.
        try:
            rows, masked = apply_mask_plan(result.columns, result.rows, decision.mask_plan)
        except ValueError as e:
            return error(f"masking failed closed: {e}")

        return AskOutcome(
            AskResponse(request_id=rid, status="ok", sql=result.sql, columns=result.columns, rows=rows,
                        masked_columns=masked),
            generated_sql=sql,
        )
