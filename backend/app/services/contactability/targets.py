"""Pure conversion of commercial leads into cautious contact discovery targets."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

from app.services.commercial_leads.service import CommercialLead
from app.services.contactability.contracts import ContactScope, ContactTarget
from app.services.scoring.company import EmployerRelationshipStatus


@dataclass(frozen=True)
class ContactIdentityContext:
    match_status: Optional[str] = None


def build_contact_targets(
    lead: CommercialLead,
    identity_context: ContactIdentityContext = ContactIdentityContext(),
) -> tuple[ContactTarget, ...]:
    """Build discovery contexts without asserting local legal establishment identity."""
    relationship = lead.scoring.employer_relationship_status
    organization_name = lead.official_name or lead.company_name
    identity_status = identity_context.match_status

    if relationship == EmployerRelationshipStatus.INTERMEDIARY:
        return (ContactTarget(
            company_key=lead.company_key,
            organization_name_snapshot=organization_name,
            scope=ContactScope.INTERMEDIARY,
            siren=lead.siren,
            local_key=None,
            local_commune_snapshot=None,
            local_location_label_snapshot=None,
            employer_relationship_status=relationship,
            identity_match_status=identity_status,
            warnings=(
                "Les coordonnées découvertes seront attribuées à l'intermédiaire, pas à un employeur final.",
            ),
            display_name_snapshot=lead.company_name,
            identity_location_snapshot=lead.principal_location,
            is_multi_local=len(lead.local_opportunities) > 1,
        ),)

    company_warnings: tuple[str, ...] = ()
    if relationship == EmployerRelationshipStatus.INTERMEDIARY_SUSPECTED:
        company_warnings = (
            "Intermédiaire ou diffuseur possible : vérification recommandée avant attribution.",
        )
    if not lead.siren:
        company_warnings += (
            "Identité juridique non confirmée : la cible reste rattachée uniquement à la clé commerciale.",
        )
    targets = [ContactTarget(
        company_key=lead.company_key,
        organization_name_snapshot=organization_name,
        scope=ContactScope.COMPANY,
        siren=lead.siren,
        local_key=None,
        local_commune_snapshot=None,
        local_location_label_snapshot=None,
        employer_relationship_status=relationship,
        identity_match_status=identity_status,
        warnings=company_warnings,
        display_name_snapshot=lead.company_name,
        identity_location_snapshot=lead.principal_location,
        is_multi_local=len(lead.local_opportunities) > 1,
    )]

    if relationship != EmployerRelationshipStatus.DIRECT_EMPLOYER:
        return tuple(targets)

    for local in lead.local_opportunities:
        warnings = [
            "La sous-opportunité locale est descriptive et ne constitue pas une identité d'établissement juridique.",
            "Le SIREN global n'est pas attribué à cette cible locale.",
        ]
        if local.commune is None:
            warnings.append(
                "Localisation non structurée : la portée locale est moins précise et doit être vérifiée."
            )
        targets.append(ContactTarget(
            company_key=lead.company_key,
            organization_name_snapshot=lead.company_name,
            scope=ContactScope.LOCAL,
            siren=None,
            local_key=local.local_key,
            local_commune_snapshot=local.commune,
            local_location_label_snapshot=local.location_label,
            employer_relationship_status=relationship,
            identity_match_status=identity_status,
            warnings=tuple(warnings),
            display_name_snapshot=lead.company_name,
            identity_location_snapshot=local.location_label,
            is_multi_local=False,
        ))
    return tuple(targets)


def is_vague_local_target(target: ContactTarget) -> bool:
    """A missing structured commune is sufficient to require local review."""
    return target.scope == ContactScope.LOCAL and target.local_commune_snapshot is None
