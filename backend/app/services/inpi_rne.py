"""Optional, targeted Data INPI RNE domain declarations.

Contract: INPI's official *Accéder aux formalités données saisies JSON*, v5.0
(August 2026), sections B/C and annex 1. A declaration is a lead, never a
verified website or recruitment offer.
"""

from __future__ import annotations

from dataclasses import dataclass
import re
from urllib.parse import urlsplit

import httpx


BASE_URL = "https://registre-national-entreprises.inpi.fr/api"
_DOMAIN = re.compile(r"^(?=.{4,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$")


class InpiUnavailable(Exception):
    """A remote/auth/schema failure. The discovery run must continue."""


class RneNotFound(InpiUnavailable):
    """The SIREN has no record in the targeted RNE endpoint."""


class RneNotReusable(InpiUnavailable):
    """The available record must not be used for this commercial resolver."""


@dataclass(frozen=True)
class RneDomain:
    siren: str
    domain: str
    effective_date: str | None
    source_path: str
    source_updated_at: str | None = None


def declared_domains(payload: object, siren: str) -> tuple[RneDomain, ...]:
    if not isinstance(payload, dict):
        raise InpiUnavailable("RNE identity mismatch")
    wrapped = isinstance(payload.get("formality"), dict)
    record = payload["formality"] if wrapped else payload
    prefix = "formality." if wrapped else ""
    if record.get("siren") != siren or (wrapped and payload.get("siren") != siren):
        raise InpiUnavailable("RNE identity mismatch")
    if record.get("diffusionINSEE") == "N":
        raise RneNotReusable("RNE dissemination restricted")
    content = record.get("content")
    if not isinstance(content, dict):
        raise InpiUnavailable("RNE content missing")
    if record.get("diffusionCommerciale", content.get("diffusionCommerciale")) is not True:
        raise RneNotReusable("RNE commercial reuse opposed or unknown")
    # For commercial prospecting, use only legal entities. No personal data
    # from the individual-proprietor branch is used for this resolver.
    legal = content.get("personneMorale")
    if not isinstance(legal, dict):
        raise RneNotReusable("RNE legal entity unavailable")
    identity = legal.get("identite")
    paths = [(prefix + "content.personneMorale.identite.nomsDeDomaine",
              identity.get("nomsDeDomaine") if isinstance(identity, dict) else None)]
    for branch in ("etablissementPrincipal", "autresEtablissements"):
        value = legal.get(branch)
        rows = value if isinstance(value, list) else [value]
        for index, row in enumerate(rows):
            if isinstance(row, dict):
                paths.append((f"{prefix}content.personneMorale.{branch}[{index}].nomsDeDomaine", row.get("nomsDeDomaine")))
    found: dict[str, RneDomain] = {}
    for path, rows in paths:
        if not isinstance(rows, list):
            continue
        for row in rows:
            if not isinstance(row, dict):
                continue
            value = row.get("nomDomaine")
            if not isinstance(value, str):
                continue
            domain = value.strip().lower().rstrip(".")
            if value.startswith(("http://", "https://")):
                parsed = urlsplit(value)
                if parsed.path not in ("", "/") or parsed.query or parsed.username or parsed.password:
                    continue
                domain = (parsed.hostname or "").lower().rstrip(".")
            if not _DOMAIN.fullmatch(domain):
                continue
            date = row.get("dateEffet")
            updated = payload.get("updatedAt") if wrapped else None
            found.setdefault(domain, RneDomain(siren, domain, date if isinstance(date, str) else None,
                                               path, updated if isinstance(updated, str) else None))
    return tuple(found.values())


class InpiRneClient:
    def __init__(self, username: str, password: str, *, requester=None, timeout_seconds: float = 30.0):
        self.username = username
        self.password = password
        self.requester = requester or httpx.request
        self.token: str | None = None
        self.timeout_seconds = timeout_seconds

    def domains_for_siren(self, siren: str) -> tuple[RneDomain, ...]:
        if not re.fullmatch(r"\d{9}", siren):
            return ()
        try:
            if self.token is None:
                login = self.requester("POST", f"{BASE_URL}/sso/login",
                                       json={"username": self.username, "password": self.password}, timeout=10)
                if login.status_code != 200:
                    raise InpiUnavailable("RNE authentication failed")
                auth = login.json()
                token = auth.get("token") if isinstance(auth, dict) else None
                if not isinstance(token, str) or not token:
                    raise InpiUnavailable("RNE token missing")
                self.token = token
            response = self.requester("GET", f"{BASE_URL}/companies/{siren}",
                                      headers={"Authorization": f"Bearer {self.token}"}, timeout=self.timeout_seconds)
            if response.status_code == 404:
                raise RneNotFound("RNE SIREN not found")
            if response.status_code != 200:
                raise InpiUnavailable(f"RNE response status {response.status_code}")
            return declared_domains(response.json(), siren)
        except (httpx.HTTPError, ValueError, TypeError, KeyError) as exc:
            raise InpiUnavailable("RNE request failed") from exc
