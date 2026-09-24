"""Deterministic, read-only commercial angle selection from local evidence.

Codes and short questions are preparation data, never a finished message.
Only explicitly approved catalog fields can enter client-safe claims.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from sqlalchemy.orm import Session

from app.services.commercial_configuration import (
    CommercialOffer, CommercialPolicy, CommercialProfile, list_offers, read_policy,
    read_profile,
)
from app.services.commercial_leads.approach import (
    ApproachOffer, ApproachReadiness, CommercialApproachContext,
    get_commercial_approach_context,
)


@dataclass(frozen=True)
class AngleClaim:
    claim: str
    claim_type: str
    source_type: str
    source_reference: str
    confidence: str
    client_communicable: bool
    limitations: tuple[str, ...] = ()


@dataclass(frozen=True)
class QualificationQuestion:
    question: str
    based_on: str
    conditional: bool = False


@dataclass(frozen=True)
class CommercialAngle:
    company_key: str
    company_name: str
    readiness: str
    pack_status: str
    active_angle: bool
    contact_rationale: Optional[str]
    entry_offer: ApproachOffer
    entry_offer_reasons: tuple[str, ...]
    specialty_match: str
    specialty_label: Optional[str]
    specialty_reason: str
    territorial_relevance: str
    territorial_reason: str
    primary_angle: Optional[str]
    secondary_angles: tuple[str, ...]
    primary_value_proposition: Optional[str]
    secondary_value_propositions: tuple[str, ...]
    target_role: Optional[str]
    target_status: str
    channel_strategy: str
    qualification_questions: tuple[QualificationQuestion, ...]
    hypotheses_as_questions: tuple[str, ...]
    claims: tuple[AngleClaim, ...]
    client_safe_facts: tuple[AngleClaim, ...]
    internal_advice: tuple[str, ...]
    do_not_claim: tuple[str, ...]
    selected_offer_code: Optional[str]
    suggested_offer_code: Optional[str]
    commercial_levers_available: tuple[str, ...]
    reasons: tuple[str, ...]


def _norm(value: str) -> str:
    plain = unicodedata.normalize("NFKD", value).casefold()
    return re.sub(r"[^a-z0-9]+", " ", "".join(ch for ch in plain if not unicodedata.combining(ch))).strip()


def _contains(text: str, phrase: str) -> bool:
    return bool(phrase and re.search(rf"(?:^| ){re.escape(phrase)}(?: |$)", text))


# Generic occupational vocabulary. It grants no expertise by itself: the local
# profile must independently assert a personal specialty in the matching family.
_ROLE_FAMILIES = (
    (
        ("it", "informatique", "numerique", "numeriques", "data"),
        ("developpeur", "developpeuse", "software", "devops", "data scientist", "data engineer",
         "analyste donnees", "cybersecurite", "administrateur systeme", "ingenieur informatique"),
    ),
    (
        ("automobile", "garage", "vehicule"),
        ("carrossier", "carrossiere", "automobile", "vehicule", "poids lourds"),
    ),
    (
        ("medical", "medico social", "sante", "soins"),
        ("ide", "infirmier", "infirmiere", "aide soignant", "aide soignante",
         "auxiliaire de vie", "auxiliaire de puericulture", "educateur specialise"),
    ),
)


def specialty_for_role(title: str, profile: Optional[CommercialProfile]) -> tuple[str, Optional[str], str]:
    """Match an occupation, never the employer's industry or an imagined mission."""
    if profile is None:
        return "no_specialty_claim", None, "profile_unavailable"
    role = _norm(title)
    for specialty in profile.specialties:
        if specialty.expertise_scope != "personal":
            continue
        if any(_contains(role, _norm(related)) for related in specialty.related_roles):
            return "strong_specialty_match", specialty.label, "personal_related_role_match"
        label = _norm(specialty.label)
        for family_labels, role_terms in _ROLE_FAMILIES:
            if any(_contains(label, term) for term in family_labels) and any(
                _contains(role, term) for term in role_terms
            ):
                return "strong_specialty_match", specialty.label, "personal_occupation_family_match"
        if len(label) > 3 and _contains(role, label):
            return "adjacent_specialty", specialty.label, "label_overlap_requires_role_confirmation"
    return "no_specialty_claim", None, "occupation_outside_personal_specialties"


def _territory(context: CommercialApproachContext, profile: Optional[CommercialProfile]) -> tuple[str, str]:
    if profile is None or not context.entry_offer.location:
        return "irrelevant", "territory_or_job_location_unavailable"
    location = _norm(context.entry_offer.location)
    departments = tuple(item.department_code for item in profile.territories if item.territorial_familiarity)
    if not any(re.search(rf"(?:^| ){re.escape(code.casefold())}(?: |$)", location) for code in departments):
        return "irrelevant", "job_outside_documented_territory"
    role = _norm(context.entry_offer.title)
    if any(_contains(role, marker) for marker in ("national", "nationale", "teletravail", "remote")):
        return "irrelevant", "national_or_remote_role_without_local_need"
    if any(term in role for term in ("domicile", "itinera", "auxiliaire de vie", "aide a domicile", "automobile", "livreur")):
        return "useful", "local_or_mobile_occupation_in_documented_territory"
    return "secondary", "job_in_documented_territory_without_proven_mobility_constraint"


def _value(context: CommercialApproachContext, match: str) -> tuple[str, tuple[str, ...], str]:
    role = _norm(context.entry_offer.title)
    if any(term in role for term in ("junior", "debutant", "alternan")):
        return "profile_criteria_clarification", ("targeted_search",), "junior_role_calls_for_autonomy_clarification"
    if any(term in role for term in ("data scientist", "ingenieur", "technicien", "developpeur")):
        return "profile_criteria_clarification", ("targeted_search", "preselection"), "technical_role_needs_explicit_criteria"
    if match == "strong_specialty_match":
        return "targeted_search", ("preselection",), "personal_specialty_supports_targeted_search"
    return "profile_criteria_clarification", ("targeted_search", "preselection"), "method_based_approach_outside_specialty"


def _questions(context: CommercialApproachContext) -> tuple[QualificationQuestion, ...]:
    role = _norm(context.entry_offer.title)
    questions = [
        QualificationQuestion("Le recrutement pour ce poste est-il toujours en cours ?", "observed_offer_status_unconfirmed"),
        QualificationQuestion("Quel critère fera réellement la différence entre les profils ?", "observed_job_title"),
    ]
    if any(term in role for term in ("junior", "debutant", "alternan")):
        extra = "Quel niveau d'autonomie attendez-vous dès l'arrivée ?"
    elif any(term in role for term in ("domicile", "itinera", "auxiliaire de vie")):
        extra = "Quel périmètre géographique le poste couvre-t-il réellement ?"
    elif any(term in role for term in ("technicien", "mecanicien", "data scientist", "developpeur")):
        extra = "Quelle compétence doit être maîtrisée dès l'arrivée ?"
    else:
        extra = "Quel point du processus actuel souhaitez-vous compléter ?"
    questions.append(QualificationQuestion(extra, "occupation_specific_clarification", True))
    return tuple(questions)


def _target(context: CommercialApproachContext) -> tuple[str, str, str]:
    selected = next((point for point in context.contacts if point.id == context.recommended_contact_id), None)
    size = re.fullmatch(r"\s*(\d+)\s*[-–]\s*(\d+)\s*", context.employee_range or "")
    small_structure = bool(size and int(size.group(2)) <= 19)
    if selected and selected.role in {"hr", "recruitment", "recruitment_service"}:
        return "responsable RH ou recrutement", "verified_function", context.recommended_channel
    if selected and selected.role == "general_routing":
        role = "dirigeant ou responsable du recrutement" if small_structure else "responsable du recrutement"
        return role, "function_to_request", f"{context.recommended_channel}:routing"
    if selected:
        return "responsable du recrutement", "function_to_request", context.recommended_channel
    if small_structure:
        return "dirigeant ou responsable du recrutement", "function_to_request", "channel_missing_or_verification_required"
    return "responsable du recrutement", "function_to_request", "channel_missing_or_verification_required"


def _pack_status(context: CommercialApproachContext) -> str:
    return {
        ApproachReadiness.READY_TO_CONTACT: "ready_for_call",
        ApproachReadiness.ROUTING_REQUIRED: "routing_required",
        ApproachReadiness.CHANNEL_MISSING: "prepared_channel_missing",
        ApproachReadiness.VERIFY_CONTACT: "verify_contact",
        ApproachReadiness.VERIFY_OFFER: "verify_offer",
        ApproachReadiness.VERIFY_EMPLOYER: "verify_employer",
        ApproachReadiness.INTERMEDIARY_NOT_EMPLOYER: "suspended",
        ApproachReadiness.SUSPENDED: "suspended",
    }[context.readiness]


def build_commercial_angle(
    context: CommercialApproachContext,
    profile: Optional[CommercialProfile],
    catalog: tuple[CommercialOffer, ...],
    policy: Optional[CommercialPolicy],
) -> CommercialAngle:
    """Pure decision function. Unknown and unapproved values remain internal."""
    suspended = context.readiness in {ApproachReadiness.SUSPENDED, ApproachReadiness.INTERMEDIARY_NOT_EMPLOYER}
    communication_blocked = context.readiness in {
        ApproachReadiness.SUSPENDED, ApproachReadiness.INTERMEDIARY_NOT_EMPLOYER,
        ApproachReadiness.VERIFY_EMPLOYER, ApproachReadiness.VERIFY_OFFER,
    } or "publication_over_30_days" in context.entry_offer.warnings
    match, label, match_reason = specialty_for_role(context.entry_offer.title, profile)
    territory, territory_reason = _territory(context, profile)
    primary, secondary, value_reason = _value(context, match)
    target_role, target_status, channel = _target(context)
    starter = next((item for item in catalog if item.code == "starter" and item.enabled_for_prospecting), None)
    enhanced = next((item for item in catalog if item.code == "enhanced" and item.enabled_for_prospecting), None)
    selected_code = starter.code if starter else None
    multisite = context.distinct_local_need_count >= 2 and context.employer_attribution == "direct_employer_plausible"
    suggested_code = enhanced.code if enhanced and multisite and not suspended else None
    reasons = [*context.reasons, match_reason, territory_reason, value_reason]
    if selected_code is None:
        reasons.append("starter_unavailable_in_catalog")
    if suggested_code:
        reasons.append("enhanced_suggested_for_observed_multiple_locations_manual_choice")
    if communication_blocked:
        reasons.append("client_communication_blocked_by_readiness")

    claims: list[AngleClaim] = []
    for fact in context.usable_facts:
        if fact.code == "multiple_canonical_needs_observed":
            continue  # Listing count must never become a vacancy count.
        verified = fact.code.startswith("confirmed_") and context.identity_match_status == "matched_high_confidence"
        claim_type = "verified_company_fact" if verified else "observed_job_fact"
        if fact.code in {"observed_company_name", "confirmed_legal_name", "confirmed_siren"}:
            claim_type = "verified_company_fact" if verified else "observed_company_fact"
        claims.append(AngleClaim(
            fact.value, claim_type, "commercial_approach_context",
            fact.source_urls[0] if fact.source_urls else fact.code,
            "verified" if verified else "observed", not communication_blocked,
            ("observed_only_not_current_opening",) if fact.code == "observed_job_title" else (),
        ))
    if match == "strong_specialty_match" and profile and label:
        claims.append(AngleClaim(label, "consultant_profile_fact", "local_commercial_profile",
                                 f"specialties:{_norm(label)}", "self_declared", not communication_blocked,
                                 ("domain_specialty_not_prior_recruitment_or_technical_assessment",)))
    if territory != "irrelevant" and profile:
        territory_code = next((item.department_code for item in profile.territories
                               if item.territorial_familiarity and re.search(
                                   rf"(?:^| ){re.escape(item.department_code.casefold())}(?: |$)",
                                   _norm(context.entry_offer.location or ""))), None)
        claims.append(AngleClaim(
            f"territorial_familiarity:{territory_code}", "consultant_profile_fact", "local_commercial_profile",
            f"territories:{territory_code}", "self_declared", not communication_blocked,
            ("no_travel_time_or_local_difficulty_inferred",),
        ))
    internal_advice = ["validate_price_discount_guarantee_and_exclusivity_manually"]
    if starter:
        for field, scope in sorted(starter.communication_scopes.items()):
            if field not in {"display_name", "features.phone_screen", "features.interview_screen",
                             "features.consultant_analysis", "features.reference_checks"}:
                continue
            value = starter.display_name if field == "display_name" else getattr(starter.features, field.partition(".")[2])
            if not value:
                continue
            if scope == "client_communicable":
                claims.append(AngleClaim(f"{field}={value}", "catalog_fact", "local_commercial_catalog",
                                         f"starter.{field}", "configured", not communication_blocked))
            else:
                internal_advice.append(f"catalog_field_{field}_{scope}")
    if profile is None:
        internal_advice.append("consultant_profile_missing")
    if policy is None:
        internal_advice.append("commercial_policy_missing")
    if suggested_code:
        internal_advice.append("enhanced_offer_requires_manual_choice")
    if context.readiness == ApproachReadiness.VERIFY_EMPLOYER:
        internal_advice.append("confirm_direct_employer_before_outreach")
    if "publication_over_30_days" in context.entry_offer.warnings:
        internal_advice.append("verify_old_publication_before_current_opening_claim")
    do_not = [*context.prohibited_claims, "prior_exact_role_recruitment_without_reference",
              "candidate_available_without_evidence", "unapproved_price_or_discount",
              "guarantee_included_or_complimentary_without_manual_decision",
              "exclusivity_without_manual_decision", "technical_or_clinical_evaluation_expertise_without_evidence",
              "job_listing_count_as_vacancy_count"]
    if match != "strong_specialty_match":
        do_not.append("personal_occupation_specialty_claim")
    if "publication_over_30_days" in context.entry_offer.warnings:
        do_not.append("current_opening_without_reverification")
    if communication_blocked:
        do_not.append("client_outreach_before_readiness_cleared")
    safe = tuple(claim for claim in claims if claim.client_communicable)
    questions = () if suspended else _questions(context)
    levers = ("pricing_manual", "non_exclusivity_for_new_client", "guarantee_possible_after_manual_review") if policy else ()
    return CommercialAngle(
        company_key=context.company_key, company_name=context.company_name,
        readiness=context.readiness,
        pack_status="verify_offer" if communication_blocked and "publication_over_30_days" in context.entry_offer.warnings
        and not suspended and context.readiness in {ApproachReadiness.READY_TO_CONTACT,
                                                     ApproachReadiness.ROUTING_REQUIRED,
                                                     ApproachReadiness.CHANNEL_MISSING}
        else _pack_status(context),
        active_angle=not suspended,
        contact_rationale=None if suspended else "observed_canonical_recruitment_need",
        entry_offer=context.entry_offer, entry_offer_reasons=context.entry_offer.selection_reasons,
        specialty_match=match, specialty_label=label, specialty_reason=match_reason,
        territorial_relevance=territory, territorial_reason=territory_reason,
        primary_angle=None if suspended else primary, secondary_angles=() if suspended else secondary,
        primary_value_proposition=None if suspended else primary,
        secondary_value_propositions=() if suspended else secondary,
        target_role=None if suspended else target_role, target_status="none" if suspended else target_status,
        channel_strategy="do_not_contact" if suspended else channel,
        qualification_questions=questions,
        hypotheses_as_questions=() if suspended else tuple(
            question.based_on for question in questions if question.conditional
        ),
        claims=tuple(claims),
        client_safe_facts=safe, internal_advice=tuple(dict.fromkeys(internal_advice)),
        do_not_claim=tuple(dict.fromkeys(do_not)), selected_offer_code=selected_code,
        suggested_offer_code=suggested_code, commercial_levers_available=levers,
        reasons=tuple(dict.fromkeys(reasons)),
    )


def get_commercial_angle(
    session: Session, company_key: str, department_code: str = "94",
    now: Optional[datetime] = None,
) -> Optional[CommercialAngle]:
    """Load local configuration once and reuse block 1's lead composition."""
    profile = read_profile(session)
    catalog = tuple(list_offers(session))
    policy = read_policy(session)
    priority = lambda title: int(specialty_for_role(title, profile)[0] == "strong_specialty_match")
    context = get_commercial_approach_context(
        session, company_key, department_code, now, specialty_priority=priority,
    )
    return build_commercial_angle(context, profile, catalog, policy) if context else None
