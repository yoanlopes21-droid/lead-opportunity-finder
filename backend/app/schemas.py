from datetime import datetime

from pydantic import BaseModel


class HealthResponse(BaseModel):
    status: str
    database: str


class AppSummary(BaseModel):
    app_name: str
    territory: str
    external_connectors_enabled: int
    contact_automation_enabled: bool
    generated_at: datetime


class FranceTravailAuthCheckResponse(BaseModel):
    status: str
    message: str
