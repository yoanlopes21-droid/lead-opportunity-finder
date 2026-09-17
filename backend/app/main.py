from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import text

from app.config import get_settings
from app.database import Base, engine
from app import models  # noqa: F401 - registers metadata
from app.api.commercial_leads import router as commercial_leads_router
from app.schemas import AppSummary, FranceTravailAuthCheckResponse, HealthResponse
from app.services.france_travail.auth import FranceTravailAuthError, FranceTravailOAuthClient

settings = get_settings()


@asynccontextmanager
async def lifespan(_: FastAPI):
    Path(settings.database_url.removeprefix("sqlite:///"),).parent.mkdir(parents=True, exist_ok=True)
    Base.metadata.create_all(bind=engine)
    yield


app = FastAPI(title=settings.app_name, version="0.1.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_credentials=False,
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)
app.include_router(commercial_leads_router)


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
