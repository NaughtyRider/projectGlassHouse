"""FastAPI application.

    uvicorn glasshouse.app:app --reload
"""

from __future__ import annotations

from fastapi import FastAPI

from glasshouse.config import Settings
from glasshouse.provider import LLMProvider, get_provider
from glasshouse.service import AskRequest, AskResponse, AskService


def create_app(settings: Settings | None = None, provider: LLMProvider | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    provider = provider or get_provider(settings)
    service = AskService(settings, provider)

    app = FastAPI(title="GlassHouse", description="Guarded text-to-SQL for AML investigations")
    app.state.service = service

    @app.get("/health")
    def health() -> dict:
        return {"status": "ok", "provider": provider.name, "db_present": settings.db_path.exists()}

    @app.post("/ask", response_model=AskResponse)
    def ask(req: AskRequest) -> AskResponse:
        return service.ask(req)

    return app


app = create_app()
