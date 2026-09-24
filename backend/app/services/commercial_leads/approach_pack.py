"""Deterministic, read-only prospecting copy from approved local evidence.

The documents are review material only. Runtime wording depends on the local
catalog's explicit communication scopes and on the selected observed offer.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from typing import Literal, Optional

from sqlalchemy.orm import Session

from app.services.commercial_configuration import (
    CommercialOffer, CommercialPolicy, CommercialProfile, list_offers, read_policy, read_profile,
)
from app.services.commercial_leads.angle import AngleClaim, CommercialAngle, build_commercial_angle, specialty_for_role
from app.services.commercial_leads.approach import ApproachOffer, CommercialApproachContext, get_commercial_approach_context


@dataclass(frozen=True)
class PhoneDraft:
    type: str
    target: str
    opening: Optional[str]
    first_30_seconds: Optional[str]
    continuation: Optional[str]
    qualification_questions: tuple[str, ...]
    meeting_transition: Optional[str]
    ready_to_copy: bool


@dataclass(frozen=True)
class EmailDraft:
    type: str
    target: str
    subject: Optional[str]
    body: Optional[str]
    attachment_recommendation: str
    ready_to_copy: bool
    required_event: Optional[str] = None


@dataclass(frozen=True)
class ObjectionResponse:
    code: str
    objection: str
    response: str
    objective: str
    follow_up_question: Optional[str]
    use_when: str
    do_not_say: tuple[str, ...]


@dataclass(frozen=True)
class PackEvidence:
    claims_used: tuple[AngleClaim, ...]
    sources: tuple[str, ...]
    warnings: tuple[str, ...]
    do_not_claim: tuple[str, ...]


@dataclass(frozen=True)
class PackInternal:
    advice: tuple[str, ...]
    commercial_levers: tuple[str, ...]
    selected_offer: Optional[str]
    unresolved_decisions: tuple[str, ...]
    attachment_if_presentation_requested: str
    attachment_if_offer_details_requested: str


@dataclass(frozen=True)
class CommercialApproachPack:
    status: str
    communication_status: str
    company: str
    entry_offer: ApproachOffer
    commercial_angle: CommercialAngle
    phone: tuple[PhoneDraft, ...]
    email: tuple[EmailDraft, ...]
    priority_objections: tuple[str, ...]
    objections: tuple[ObjectionResponse, ...]
    evidence: PackEvidence
    internal: PackInternal


def _inline(value: str, limit: int = 120) -> str:
    """Keep scraped labels as bounded data, never as instructions or paragraphs."""
    return re.sub(r"\s+", " ", value).strip()[:limit].rstrip()


def _role_label(value: str) -> str:
    return re.sub(r"\s*[-–]\s*CDI\s*(?:\(H/F\))?$", "", _inline(value), flags=re.IGNORECASE)


def _location_phrase(value: Optional[str]) -> str:
    if not value:
        return ""
    place = re.sub(r"^\s*\d{2,3}\s*[-–]\s*", "", _inline(value))
    if place.casefold() == "val-de-marne":
        return " dans le Val-de-Marne"
    if place.isupper():
        place = place.title()
    place = re.sub(r"(?i)[ -]sur[ -]", "-sur-", place)
    if place.startswith("Le "):
        return f" au {place[3:]}"
    return f" à {place}"


def _catalog_claim(angle: CommercialAngle, field: str) -> bool:
    return any(c.source_reference == f"starter.{field}" for c in angle.client_safe_facts)


def _priority_codes(angle: CommercialAngle) -> tuple[str, ...]:
    if angle.pack_status == "routing_required":
        return ("wrong_person", "send_email", "recruit_internally")
    if angle.specialty_match == "strong_specialty_match":
        return ("recruit_internally", "existing_agency", "prior_experience")
    return ("recruit_internally", "enough_applications", "why_you")


def _objections(angle: CommercialAngle) -> tuple[ObjectionResponse, ...]:
    specialty = angle.specialty_match == "strong_specialty_match" and angle.specialty_label and any(
        c.source_reference == f"specialties:{_norm_label(angle.specialty_label)}" for c in angle.client_safe_facts
    )
    if specialty:
        experience = ("Je n'ai pas de recrutement identique à vous citer de façon vérifiée. "
                      f"En revanche, le domaine {_inline(angle.specialty_label)} fait partie de mes spécialités. "
                      "Je partirais de vos critères pour construire une recherche ciblée.")
        why = (f"Le domaine {_inline(angle.specialty_label)} fait partie de mes spécialités. "
               "Je partirais de vos critères pour cibler et qualifier les profils utiles.")
    else:
        experience = ("Je n'ai pas de recrutement identique à vous citer de façon vérifiée. "
                      "Je préfère partir de vos critères métier et définir avec vous une recherche adaptée.")
        why = "Je commencerais par vos critères prioritaires, puis une recherche ciblée et une qualification adaptée au poste."
    items = [
        ("recruit_internally", "On recrute en interne", "Bien sûr. Un appui extérieur n'aurait de sens qu'en complément, si un manque apparaît.", "respecter le dispositif interne", "Avez-vous déjà les profils que vous souhaitez rencontrer ?", "si le recrutement interne est évoqué", ("unproven_recruitment_difficulty",)),
        ("existing_agency", "On travaille déjà avec un cabinet", "Si le besoin est bien couvert, je ne vais pas ajouter un intermédiaire inutile.", "respecter le cabinet en place", "Reste-t-il un point sur lequel vous cherchez une réponse ?", "si un cabinet intervient déjà", ("competitor_disparagement",)),
        ("too_expensive", "C'est trop cher", "Je comprends. Le sujet est-il le montant envisagé ou le périmètre de l'accompagnement ?", "comprendre l'objection avant toute discussion", None, "si un prix a réellement été évoqué", ("unapproved_price_or_discount",)),
        ("enough_applications", "On reçoit déjà assez de candidatures", "C'est une bonne chose. L'intérêt d'un appui dépend surtout de leur adéquation avec vos critères.", "distinguer volume et adéquation", "Avez-vous suffisamment de profils qui répondent à vos critères prioritaires ?", "si le volume de candidatures est évoqué", ("invented_candidate_shortage",)),
        ("send_email", "Envoyez-moi un mail", "Bien sûr. À quelle adresse professionnelle et à l'attention de qui puis-je l'envoyer ?", "obtenir un destinataire utile", None, "uniquement après une demande réelle pendant l'appel", ("fabricated_prior_exchange",)),
        ("no_budget", "Nous n'avons pas de budget", "Je comprends. Je ne vais pas vous pousser à engager une dépense qui n'est pas prévue.", "clarifier la possibilité d'un appui", "Un accompagnement extérieur est-il exclu ou le budget n'a-t-il pas encore été défini ?", "si l'absence de budget est exprimée", ("unapproved_price_or_discount",)),
        ("almost_filled", "Le poste est presque pourvu", "C'est une bonne nouvelle. Je vous laisse avancer avec les profils déjà identifiés.", "respecter l'avancement", None, "si le poste est presque pourvu", ("unproven_recruitment_difficulty",)),
        ("no_exclusivity", "Je ne veux pas d'exclusivité", "Je comprends. Les modalités éventuelles se discutent avant toute décision.", "ne pas promettre de condition contractuelle", "Est-ce le point principal qui vous retient ?", "si l'exclusivité est soulevée", ("exclusivity_without_manual_decision",)),
        ("advertised_everywhere", "On a déjà diffusé partout", "La diffusion est déjà couverte. Un échange n'aurait de sens que si une recherche ciblée apportait un complément.", "repositionner l'apport potentiel", "Y a-t-il un profil précis que vos annonces n'atteignent pas ?", "si les annonces sont déjà largement diffusées", ("invented_distribution_advantage",)),
        ("call_later", "Rappelez-moi plus tard", "Bien sûr, je ne vous retiens pas.", "respecter le temps du prospect", "Quel moment vous conviendrait mieux ?", "si l'interlocuteur demande un rappel", ("invented_appointment",)),
        ("wrong_person", "Je ne suis pas la bonne personne", "Merci de me le préciser. Je cherche la personne qui suit ce recrutement.", "trouver le bon interlocuteur", "Quel service ou quelle fonction dois-je demander ?", "si le destinataire ne pilote pas le besoin", ("confirmed_hr_recipient",)),
        ("why_you", "Pourquoi vous plutôt qu'un autre cabinet ?", why, "expliquer une méthode pertinente", "Quel critère compte le plus pour vous sur ce poste ?", "si une comparaison est demandée", ("unsupported_superiority",)),
        ("prior_experience", "Vous avez déjà recruté ce profil ?", experience, "répondre honnêtement sur l'expérience", "Quel savoir-faire est indispensable dès l'arrivée ?", "si une référence de mission est demandée", ("prior_exact_role_recruitment_without_reference",)),
    ]
    return tuple(ObjectionResponse(*item) for item in items)


def _norm_label(label: str) -> str:
    from app.services.commercial_leads.angle import _norm
    return _norm(label)


def build_commercial_approach_pack(
    context: CommercialApproachContext, angle: CommercialAngle,
    profile: Optional[CommercialProfile] = None,
    *, call_request_kind: Optional[Literal["email", "presentation", "offer_details", "terms"]] = None,
) -> CommercialApproachPack:
    """Compose drafts. Only pass call_request_kind from a recorded real call event."""
    status = angle.pack_status
    if angle.selected_offer_code is None and status not in {"suspended", "intermediary_not_employer", "verify_employer", "verify_offer"}:
        status = "verify_offer"
    blocked = status in {"verify_employer", "verify_offer", "intermediary_not_employer", "suspended"}
    contactable = status in {"ready_for_call", "routing_required"}
    label = _role_label(angle.entry_offer.title)
    place = _location_phrase(angle.entry_offer.location)
    role = f"{label}{place}"
    identity = f"{_inline(profile.consultant_name)}, " if profile else ""
    title = _inline(profile.commercial_title) if profile else "consultant en recrutement"
    intro = f"Bonjour, je suis {identity}{title}."
    observed = f"J'ai vu votre annonce pour le poste « {label} »{place}."
    first = f"{intro} {observed} Est-ce bien vous qui suivez ce recrutement ?"
    questions = tuple(q.question for q in angle.qualification_questions)
    transition = ("Si une recherche complémentaire peut vous être utile, je vous propose vingt minutes "
                  "pour préciser vos critères et voir si un appui ciblé a du sens.")
    continuation = "Merci. Où en êtes-vous aujourd'hui sur ce recrutement ?"
    gatekeeper_first = (f"{intro} Je vous appelle au sujet de votre annonce pour « {label} »{place}. "
                        "Pourriez-vous m'indiquer la personne ou le service qui le suit ?")
    gatekeeper_next = ("Je souhaite vérifier avec la personne qui suit ce recrutement "
                       "si un appui extérieur ciblé peut être pertinent.")
    phone = (
        PhoneDraft("phone_decision_maker", angle.target_role or "responsable du recrutement", None if blocked else intro,
                   None if blocked else first, None if blocked else continuation, questions if not blocked else (),
                   None if blocked else transition, contactable),
        PhoneDraft("phone_director", "dirigeant", None if blocked else intro,
                   None if blocked else f"{intro} {observed} Où en êtes-vous sur ce poste ?",
                   None if blocked else "Quel point est le plus important pour vous sur ce poste ?",
                   questions if not blocked else (), None if blocked else transition, contactable),
        PhoneDraft("phone_hr", "RH ou recrutement", None if blocked else intro,
                   None if blocked else f"{intro} {observed} Une recherche ciblée en complément de votre démarche pourrait-elle être utile ?",
                   None if blocked else "Où en êtes-vous dans votre recherche ?",
                   questions if not blocked else (), None if blocked else transition, contactable),
        PhoneDraft("phone_manager", "manager métier", None if blocked else intro,
                   None if blocked else f"{intro} {observed} Quel critère métier est le plus important pour vous ?",
                   None if blocked else "Qui valide un éventuel appui extérieur sur ce recrutement ?",
                   questions if not blocked else (), None if blocked else transition, contactable),
        PhoneDraft("phone_gatekeeper", "standard ou accueil", None if blocked else intro,
                   None if blocked else gatekeeper_first, None if blocked else gatekeeper_next, (), None, contactable),
        PhoneDraft("phone_wrong_person", "mauvais interlocuteur", None if blocked else "Merci de me le préciser.",
                   None, None if blocked else "Quel service ou quelle fonction suit ce recrutement ?", (), None, contactable),
        PhoneDraft("phone_busy", "personne pressée", None if blocked else "Je comprends, je ne vous retiens pas.",
                   None, None if blocked else "Quel moment vous conviendrait mieux ?", (), None, contactable),
        PhoneDraft("phone_email_request", "demande réelle par téléphone", None if blocked else "Bien sûr.",
                   None, None if blocked else "À quelle adresse professionnelle et à l'attention de qui ?", (), None, contactable),
        PhoneDraft("phone_refusal", "refus clair", None if blocked else "Je comprends, merci pour votre réponse.",
                   None, None, (), None, contactable),
    )
    subject = f"Votre recrutement : {role}"
    has_screen = _catalog_claim(angle, "features.phone_screen")
    has_hunt = _catalog_claim(angle, "features.hunt_campaign_count")
    specialty_safe = bool(angle.specialty_match == "strong_specialty_match" and angle.specialty_label and any(
        c.source_reference == f"specialties:{_norm_label(angle.specialty_label)}" for c in angle.client_safe_facts
    ))
    territory_safe = bool(angle.territorial_relevance == "useful" and any(
        c.source_reference.startswith("territories:") for c in angle.client_safe_facts
    ))
    contribution = ("une recherche ciblée avec une première qualification téléphonique des profils"
                    if has_screen else "une recherche ciblée complémentaire")
    specialty_line = f" Le domaine {_inline(angle.specialty_label)} fait partie de mes spécialités." if specialty_safe else ""
    territory_line = " Le Val-de-Marne est un territoire que je connais." if territory_safe else ""
    proposal = (f"Je peux vous proposer {contribution}, à partir des critères qui comptent pour vous."
                if has_hunt else f"Je souhaiterais voir si {contribution} pourrait compléter votre démarche.")
    cold = (f"Bonjour,\n\nJ'ai vu votre annonce pour le poste « {label} »{place}."
            f"{specialty_line}{territory_line} {proposal}\n\n"
            "Une recherche de ce type aurait-elle un intérêt pour ce poste ?")
    routing = (f"Bonjour,\n\nJe vous contacte au sujet de l'annonce pour le poste « {label} »{place}. "
               "Pourriez-vous transmettre ce message à la personne qui pilote ce recrutement ? "
               "Je souhaiterais vérifier avec elle si une recherche ciblée complémentaire pourrait être utile.")
    after_call = (f"Bonjour,\n\nComme demandé lors de notre échange, je vous écris au sujet du poste « {label} »{place}. "
                  "Je vous propose de préciser les critères prioritaires et de voir si une recherche ciblée "
                  "peut compléter vos démarches.\n\nCela vous semblerait-il utile ?")
    attachment = {"presentation": "approved_client_presentation", "offer_details": "approved_client_offer_sheet",
                  "terms": "approved_client_terms_after_manual_review"}.get(call_request_kind or "", "none")
    email = (
        EmailDraft("cold_email", angle.target_role or "responsable du recrutement", None if blocked else subject,
                   None if blocked else cold, "none", contactable and status != "routing_required"),
        EmailDraft("routing_email", "adresse générale", None if blocked else subject,
                   None if blocked else routing, "none", contactable and status == "routing_required"),
        EmailDraft("email_requested_after_call", "destinataire confirmé après appel", None if blocked else subject,
                   None if blocked or not call_request_kind else after_call, attachment,
                   contactable and bool(call_request_kind) and call_request_kind != "terms",
                   "confirmed_email_request_in_call"),
    )
    if status in {"suspended", "intermediary_not_employer"}:
        phone = tuple(PhoneDraft(p.type, p.target, None, None, None, (), None, False) for p in phone)
        email = tuple(EmailDraft(e.type, e.target, None, None, "none", False, e.required_event) for e in email)
    objections = _objections(angle) if not blocked else ()
    def was_used(claim: AngleClaim) -> bool:
        if claim.claim_type == "observed_job_fact":
            return claim.claim == angle.entry_offer.title or bool(place and claim.claim == angle.entry_offer.location)
        if claim.source_reference == "starter.features.phone_screen":
            return has_screen
        if claim.source_reference == "starter.features.hunt_campaign_count":
            return has_hunt
        if claim.source_reference.startswith("territories:"):
            return territory_safe
        return bool(angle.specialty_match == "strong_specialty_match" and angle.specialty_label
                    and claim.source_reference == f"specialties:{_norm_label(angle.specialty_label)}")
    claims = tuple(c for c in angle.client_safe_facts if was_used(c)) if not blocked else ()
    if profile and not blocked:
        claims += (
            AngleClaim(profile.consultant_name, "consultant_profile_fact", "local_commercial_profile",
                       "consultant_name", "self_declared", True),
            AngleClaim(profile.commercial_title, "consultant_profile_fact", "local_commercial_profile",
                       "commercial_title", "self_declared", True),
        )
    warnings = tuple(dict.fromkeys((*context.warnings, *angle.internal_advice,
                                    *(("channel_missing",) if status == "prepared_channel_missing" else ()))))
    internal = PackInternal(
        advice=tuple(dict.fromkeys((*angle.internal_advice, "pause_after_opening_and_listen",
                                    "ask_one_or_two_questions_not_all",
                                    "gatekeeper_request_function_and_professional_address_if_name_missing",
                                    "stop_on_clear_refusal"))),
        commercial_levers=angle.commercial_levers_available,
        selected_offer=angle.selected_offer_code,
        unresolved_decisions=("pricing_manual", "exclusivity_manual", "guarantee_manual"),
        attachment_if_presentation_requested="approved_client_presentation",
        attachment_if_offer_details_requested="approved_client_offer_sheet",
    )
    communication_status = ("blocked" if blocked else "prepared_no_channel" if status == "prepared_channel_missing"
                            else "verify_contact" if status == "verify_contact" else "communicable")
    return CommercialApproachPack(
        status=status, communication_status=communication_status, company=angle.company_name,
        entry_offer=angle.entry_offer, commercial_angle=angle, phone=phone, email=email,
        priority_objections=_priority_codes(angle) if objections else (), objections=objections,
        evidence=PackEvidence(claims, tuple(dict.fromkeys(c.source_reference for c in claims)),
                              warnings, angle.do_not_claim), internal=internal,
    )


def get_commercial_approach_pack(
    session: Session, company_key: str, department_code: str = "94", now: Optional[datetime] = None,
) -> Optional[CommercialApproachPack]:
    profile = read_profile(session)
    catalog: tuple[CommercialOffer, ...] = tuple(list_offers(session))
    policy: Optional[CommercialPolicy] = read_policy(session)
    priority = lambda title: int(specialty_for_role(title, profile)[0] == "strong_specialty_match")
    context = get_commercial_approach_context(session, company_key, department_code, now, specialty_priority=priority)
    if context is None:
        return None
    angle = build_commercial_angle(context, profile, catalog, policy)
    return build_commercial_approach_pack(context, angle, profile)
