from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import text

from app.config import get_settings
from app.database import Base, engine
from app import models  # noqa: F401 - registers metadata
from app.schemas import AppSummary, HealthResponse

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
    allow_methods=["GET"],
    allow_headers=["*"],
)


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
