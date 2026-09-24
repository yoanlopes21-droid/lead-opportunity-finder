from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import text

from app.config import get_settings
from app.database import Base, SessionLocal, engine
from app import models  # noqa: F401 - registers metadata
from app.api.commercial_leads import router as commercial_leads_router
from app.api.commercial_exclusions import router as commercial_exclusions_router
from app.api.commercial_relationships import router as commercial_relationships_router
from app.api.commercial_configuration import router as commercial_configuration_router
from app.api.brave_usage import router as brave_usage_router
from app.api.search_runs import router as search_runs_router
from app.api.job_offer_refresh_runs import router as job_offer_refresh_runs_router
from app.api.source_boards import router as source_boards_router
from app.api.open_web_runs import router as open_web_runs_router
from app.api.recruitment_signals import router as recruitment_signals_router
from app.services.search_runs import ensure_search_run_schema
from app.services.persistence.offers import ensure_collection_run_schema
from app.services.job_offer_refresh_runs import recover_orphaned_refresh_runs
from app.services.job_source_boards import recover_orphaned_board_runs
from app.services.open_web_runs import recover_orphaned_open_web_runs
from app.services.collection.open_web import reconcile_recruitment_signal_quality
from app.schemas import AppSummary, FranceTravailAuthCheckResponse, HealthResponse
from app.services.france_travail.auth import FranceTravailAuthError, FranceTravailOAuthClient

settings = get_settings()


@asynccontextmanager
async def lifespan(_: FastAPI):
    Path(settings.database_url.removeprefix("sqlite:///"),).parent.mkdir(parents=True, exist_ok=True)
    Base.metadata.create_all(bind=engine)
    ensure_search_run_schema(engine)
    ensure_collection_run_schema(engine)
    with SessionLocal() as session:
        reconcile_recruitment_signal_quality(session)
        recover_orphaned_refresh_runs(session)
        recover_orphaned_board_runs(session)
        recover_orphaned_open_web_runs(session)
    yield


app = FastAPI(title=settings.app_name, version="0.1.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_credentials=False,
    allow_methods=["GET", "POST", "PATCH", "DELETE"],
    allow_headers=["*"],
)
app.include_router(commercial_leads_router)
app.include_router(commercial_exclusions_router)
app.include_router(commercial_relationships_router)
app.include_router(commercial_configuration_router)
app.include_router(brave_usage_router)
app.include_router(search_runs_router)
app.include_router(job_offer_refresh_runs_router)
app.include_router(source_boards_router)
app.include_router(open_web_runs_router)
app.include_router(recruitment_signals_router)


@app.get("/api/v1/health", response_model=HealthResponse, tags=["system"])
def health() -> HealthResponse:
    with engine.connect() as connection:
        connection.execute(text("SELECT 1"))
    return HealthResponse(status="ok", database="connected")


@app.get("/api/v1/summary", response_model=AppSummary, tags=["system"])
def summary() -> AppSummary:
    return AppSummary(
        app_name=settings.app_name,
        territory="Val-de-Marne (94)",
        external_connectors_enabled=0,
        contact_automation_enabled=False,
        generated_at=datetime.now(timezone.utc),
    )


@app.post(
    "/api/v1/sources/france-travail/auth-check",
    response_model=FranceTravailAuthCheckResponse,
    tags=["sources"],
)
def check_france_travail_authentication() -> FranceTravailAuthCheckResponse:
    """Verify OAuth2 credentials without returning a token or querying job offers."""
    client = FranceTravailOAuthClient(settings)
    if not client.is_configured:
        return FranceTravailAuthCheckResponse(
            status="not_configured",
            message="France Travail credentials are not configured locally.",
        )

    try:
        client.get_access_token()
    except FranceTravailAuthError:
        return FranceTravailAuthCheckResponse(
            status="authentication_failed",
            message="France Travail authentication failed. Check local configuration and API access.",
        )
    return FranceTravailAuthCheckResponse(
        status="authenticated",
        message="France Travail authentication succeeded. No job search was performed.",
    )
