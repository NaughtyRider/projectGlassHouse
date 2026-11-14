"""FastAPI application.

    uvicorn glasshouse.app:app --reload
"""

from __future__ import annotations

from fastapi import FastAPI, Query
from fastapi.responses import JSONResponse

from glasshouse.audit import AuditError, AuditLog
from glasshouse.config import Settings
from glasshouse.policy import AccessPolicy
from glasshouse.provider import LLMProvider, get_provider
from glasshouse.service import AskRequest, AskResponse, AskService

MAX_AUDIT_LIMIT = 500


def create_app(
    settings: Settings | None = None,
    provider: LLMProvider | None = None,
    policy: AccessPolicy | None = None,
    audit: AuditLog | None = None,
) -> FastAPI:
    settings = settings or Settings.from_env()
    provider = provider or get_provider(settings)
    policy = policy or AccessPolicy.load(settings.policy_path)  # invalid policy -> fail at startup
    audit = audit or AuditLog(settings.audit_db_path)
    if audit.path.resolve() == settings.db_path.resolve():
        raise ValueError("audit database must be a different file from the ledger database")
    service = AskService(settings, provider, policy, audit)

    app = FastAPI(title="GlassHouse", description="Guarded text-to-SQL for AML investigations")
    app.state.service = service

    @app.get("/health")
    def health() -> dict:
        return {"status": "ok", "provider": provider.name, "db_present": settings.db_path.exists(),
                "roles": sorted(policy.roles)}

    @app.post("/ask", response_model=AskResponse, responses={403: {"model": AskResponse}})
    def ask(req: AskRequest):
        outcome = service.ask(req)
        if outcome.http_status != 200:
            return JSONResponse(status_code=outcome.http_status, content=outcome.response.model_dump())
        return outcome.response

    @app.get("/audit")
    def recent_audit(limit: int = Query(50, ge=1, le=MAX_AUDIT_LIMIT)):
        try:
            records = audit.recent(limit)
        except AuditError as e:
            return JSONResponse(status_code=503, content={"detail": str(e)})
        return {"count": len(records), "records": records}

    return app


app = create_app()
