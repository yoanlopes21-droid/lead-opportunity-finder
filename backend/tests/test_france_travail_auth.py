import httpx
import pytest

from app.config import Settings
from app.services.france_travail.auth import FranceTravailAuthError, FranceTravailOAuthClient


class FakeResponse:
    def __init__(self, payload: dict):
        self.payload = payload

    def raise_for_status(self) -> None:
        pass

    def json(self) -> dict:
        return self.payload


def configured_settings() -> Settings:
    return Settings(
        _env_file=None,
        france_travail_client_id="test-client-id",
        france_travail_client_secret="test-client-secret",
    )


def test_access_token_uses_client_credentials_and_caches_result(monkeypatch):
    calls = []

    def fake_post(*args, **kwargs):
        calls.append((args, kwargs))
        return FakeResponse({"access_token": "test-token", "expires_in": 1500})

    monkeypatch.setattr(httpx, "post", fake_post)
    client = FranceTravailOAuthClient(configured_settings())

    assert client.get_access_token() == "test-token"
    assert client.get_access_token() == "test-token"
    assert len(calls) == 1
    assert calls[0][1]["data"]["grant_type"] == "client_credentials"
    assert calls[0][1]["data"]["scope"] == "api_offresdemploiv2 o2dsoffre"


def test_access_token_fails_without_local_credentials():
    with pytest.raises(FranceTravailAuthError):
        FranceTravailOAuthClient(Settings(_env_file=None)).get_access_token()


def test_auth_check_does_not_return_tokens_to_callers(monkeypatch):
    from fastapi.testclient import TestClient
    from app import main

    monkeypatch.setattr(
        main,
        "settings",
        Settings(
            _env_file=None,
            france_travail_client_id=None,
            france_travail_client_secret=None,
        ),
    )

    with TestClient(main.app) as client:
        response = client.post("/api/v1/sources/france-travail/auth-check")

    assert response.status_code == 200
    assert response.json()["status"] == "not_configured"
    assert "token" not in response.text.lower()
