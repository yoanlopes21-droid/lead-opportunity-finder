"""OAuth2 client-credentials support for the official France Travail API."""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Optional

import httpx

from app.config import Settings


class FranceTravailAuthError(Exception):
    """Raised when an access token cannot be obtained without leaking credentials."""


@dataclass(frozen=True)
class AccessToken:
    value: str
    expires_at_monotonic: float

    def is_valid(self) -> bool:
        return time.monotonic() < self.expires_at_monotonic


class FranceTravailOAuthClient:
    """Backend-only OAuth2 client. Access tokens are kept in memory, never persisted."""

    def __init__(self, settings: Settings):
        self._settings = settings
        self._access_token: Optional[AccessToken] = None

    @property
    def is_configured(self) -> bool:
        return bool(
            self._settings.france_travail_client_id
            and self._settings.france_travail_client_secret
        )

    def get_access_token(self) -> str:
        if not self.is_configured:
            raise FranceTravailAuthError("France Travail credentials are not configured.")

        if self._access_token and self._access_token.is_valid():
            return self._access_token.value

        try:
            response = httpx.post(
                self._settings.france_travail_token_url,
                data={
                    "grant_type": "client_credentials",
                    "client_id": self._settings.france_travail_client_id,
                    "client_secret": self._settings.france_travail_client_secret,
                    "scope": self._settings.france_travail_scope,
                },
                headers={"Accept": "application/json"},
                timeout=self._settings.france_travail_timeout_seconds,
            )
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise FranceTravailAuthError("France Travail authentication failed.") from exc

        token = payload.get("access_token")
        if not isinstance(token, str) or not token:
            raise FranceTravailAuthError("France Travail returned no access token.")

        expires_in = payload.get("expires_in", 0)
        try:
            cache_seconds = max(float(expires_in) - 60, 0)
        except (TypeError, ValueError):
            cache_seconds = 0
        self._access_token = AccessToken(token, time.monotonic() + cache_seconds)
        return token
