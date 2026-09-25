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
    meeting_transition_after_response: Optional[str] = None


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
    communication_title: str
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
    """Remove only editorial jobboard suffixes and unambiguous role duplicates."""
    label = _inline(value)
    label = re.sub(r"\s*[-–]\s*CDI\s*(?:\(H/F\))?$", "", label, flags=re.IGNORECASE)
    label = re.sub(r"\s*\((?:H/F|F/H)\)\s*$|\s+(?:H/F|F/H)\s*$", "", label, flags=re.IGNORECASE)
    specific = re.fullmatch(r"Informaticien(?:ne)? d['’]étude\s*[-–]\s*(Data Scientist junior)", label, re.I)
    if specific:
        return specific.group(1)
    if re.fullmatch(r"Mécanicien\s*/\s*Mécanicienne automobile", label, re.I):
        return "Mécanicien automobile"
    if re.fullmatch(r"Mécanicienne\s*/\s*Mécanicien Moteur", label, re.I):
        return "Mécanicien moteur"
    if re.fullmatch(r"Infirmier\(ère\) Conseil", label, re.I):
        return "Infirmier conseil"
    return label


def _specialty_phrase(label: str) -> str:
    normalized = _norm_label(label)
    if normalized in {"it", "technologies numeriques", "informatique"}:
        return "Je travaille notamment sur les recrutements IT."
    if normalized in {"automobile", "mobilite vehicule"}:
        return "Je travaille notamment sur les métiers de l'automobile."
    if normalized in {"medical medico social", "medico social medical"}:
        return "Je travaille notamment sur les métiers médicaux et médico-sociaux."
    if normalized == "services a domicile":
        return "Je travaille notamment sur les métiers de l'aide à domicile."
    return f"Je travaille aussi sur le domaine « {_inline(label)} »."


def _description_detail(context: CommercialApproachContext) -> Optional[tuple[str, AngleClaim]]:
    """One fixed, literal detail from the selected offer's sourced excerpt."""
    entry = context.entry_offer
    if not entry.description_excerpt or not entry.source_urls:
        return None
    description = entry.description_excerpt.casefold()
    role = _role_label(entry.title).casefold()
    candidates = (
        ("machine learning", "data scientist", "Machine Learning", "Vous y mentionnez le Machine Learning."),
        ("python", "data scientist", "Python", "Vous y mentionnez Python."),
        ("sql", "data scientist", "SQL", "Vous y mentionnez SQL."),
        ("machines à bois", "maintenance", "machines à bois", "Vous mentionnez la maintenance de machines à bois."),
        ("sécurité affective et physique", "puériculture", "sécurité affective et physique", "Vous mettez l'accent sur la sécurité affective et physique des enfants."),
        ("gestes du quotidien", "auxiliaire de vie", "gestes du quotidien", "Vous parlez de l'accompagnement dans les gestes du quotidien."),
        ("retour à domicile", "infirmier conseil", "retour à domicile", "Vous mentionnez le suivi jusqu'au retour à domicile."),
    )
    for marker, role_marker, claim_text, sentence in candidates:
        if role_marker in role and re.search(rf"(?<!\w){re.escape(marker)}(?!\w)", description):
            return sentence, AngleClaim(
                claim_text, "observed_job_description_fact", "selected_job_description",
                f"{entry.source}:{entry.offer_id}", "observed", True,
                ("selected_offer_description_excerpt_only",),
            )
    return None


def _spoken_questions(angle: CommercialAngle) -> tuple[str, ...]:
    role = _role_label(angle.entry_offer.title).casefold()
    questions = []
    for item in angle.qualification_questions:
        if item.based_on == "observed_offer_status_unconfirmed":
            questions.append("Vous êtes toujours en recherche sur ce poste ?")
        elif item.based_on == "observed_job_title":
            questions.append("Sur ce poste, qu'est-ce qui compte le plus pour vous ?")
        elif "autonomie" in item.question.casefold() and "junior" in role:
            questions.append("Sur un profil junior, quelle autonomie attendez-vous dès le départ ?")
        elif "périmètre géographique" in item.question:
            questions.append("Concrètement, quel secteur la personne devra couvrir ?")
        elif "compétence" in item.question.casefold():
            questions.append("Qu'est-ce que la personne doit savoir faire dès son arrivée ?")
        else:
            questions.append(item.question)
    return tuple(questions)


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
    return any(c.source_reference == f"{angle.selected_offer_code}.{field}" for c in angle.client_safe_facts)


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
    hunt_safe = _catalog_claim(angle, "features.hunt_campaign_count")
    screen_safe = _catalog_claim(angle, "features.phone_screen")
    method = ("chercher et qualifier des profils ciblés" if hunt_safe and screen_safe
              else "mener une recherche ciblée sur vos critères" if hunt_safe
              else "voir si une recherche ciblée serait utile")
    if specialty:
        experience = ("Je n'ai pas de mission exactement identique à vous citer, donc je préfère être transparent. "
                      f"{_specialty_phrase(angle.specialty_label)} "
                      f"Je partirais de vos critères pour {method}.")
        why = (f"{_specialty_phrase(angle.specialty_label)} "
               f"Mon apport serait de partir de vos critères, puis de {method}.")
    else:
        experience = ("Je n'ai pas de mission exactement identique à vous citer, donc je préfère être transparent. "
                      f"Je cadrerais avec vous les critères indispensables avant de {method} ; "
                      "la validation métier resterait avec vous.")
        why = ("Je ne vais pas prétendre connaître votre métier mieux que vous. "
               "Mon apport serait de cadrer les critères indispensables avec vous, "
               f"puis de {method}.")
    if not specialty and angle.territorial_relevance == "useful" and any(
        c.source_reference.startswith("territories:") for c in angle.client_safe_facts
    ):
        why = "Je travaille notamment sur le Val-de-Marne. " + why
    items = [
        ("recruit_internally", "On recrute en interne", "Je comprends. L'idée est juste de compléter votre recherche si vous en avez besoin.", "respecter le dispositif interne", "Sur ce poste, vous avez déjà assez de profils que vous souhaitez rencontrer ?", "si le recrutement interne est évoqué", ("unproven_recruitment_difficulty",)),
        ("existing_agency", "On travaille déjà avec un cabinet", "Je comprends. Si ça fonctionne bien, je ne vais pas vous proposer de changer pour changer.", "respecter le cabinet en place", "Sur ce poste, avez-vous déjà assez de profils pertinents à rencontrer ?", "si un cabinet intervient déjà", ("competitor_disparagement",)),
        ("too_expensive", "C'est trop cher", "Je comprends. Qu'est-ce qui vous paraît trop élevé : le montant ou ce qui est compris ?", "comprendre l'objection avant toute discussion", None, "si un prix a réellement été évoqué", ("unapproved_price_or_discount",)),
        ("enough_applications", "On reçoit déjà assez de candidatures", "C'est une bonne nouvelle. Reste à voir si ces candidatures correspondent à ce que vous cherchez.", "distinguer volume et adéquation", "Vous avez assez de profils qui répondent à vos critères essentiels ?", "si le volume de candidatures est évoqué", ("invented_candidate_shortage",)),
        ("send_email", "Envoyez-moi un mail", "Bien sûr. À quelle adresse professionnelle et à l'attention de qui puis-je l'envoyer ?", "obtenir un destinataire utile", None, "uniquement après une demande réelle pendant l'appel", ("fabricated_prior_exchange",)),
        ("no_budget", "Nous n'avons pas de budget", "Je comprends. Je ne vais pas vous pousser sur un budget qui n'est pas prévu.", "clarifier la possibilité d'un appui", "Un appui extérieur est exclu sur ce poste, ou le budget n'est pas encore fixé ?", "si l'absence de budget est exprimée", ("unapproved_price_or_discount",)),
        ("almost_filled", "Le poste est presque pourvu", "Tant mieux. Je vous laisse avancer sur cette piste.", "respecter l'avancement", None, "si le poste est presque pourvu", ("unproven_recruitment_difficulty",)),
        ("no_exclusivity", "Je ne veux pas d'exclusivité", "Je comprends. On peut regarder ce point avant de parler d'une collaboration.", "ne pas promettre de condition contractuelle", "C'est le principal point qui vous retient ?", "si l'exclusivité est soulevée", ("exclusivity_without_manual_decision",)),
        ("advertised_everywhere", "On a déjà diffusé partout", "Je comprends. Je ne vais pas vous proposer de rediffuser la même annonce.", "repositionner l'apport potentiel", "Y a-t-il un profil précis que vos annonces n'atteignent pas ?", "si les annonces sont déjà largement diffusées", ("invented_distribution_advantage",)),
        ("call_later", "Rappelez-moi plus tard", "Bien sûr, je ne vous retiens pas.", "respecter le temps du prospect", "Quel moment vous conviendrait mieux ?", "si l'interlocuteur demande un rappel", ("invented_appointment",)),
        ("wrong_person", "Je ne suis pas la bonne personne", "Merci de me le dire. Je cherche la personne qui suit ce recrutement.", "trouver le bon interlocuteur", "Quel service ou quelle fonction dois-je demander ?", "si le destinataire ne pilote pas le besoin", ("confirmed_hr_recipient",)),
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
    confirmed_priority: Optional[str] = None,
) -> CommercialApproachPack:
    """Compose drafts. Call details must come from a recorded real exchange."""
    status = angle.pack_status
    if angle.selected_offer_code is None and status not in {"suspended", "intermediary_not_employer", "verify_employer", "verify_offer"}:
        status = "verify_offer"
    blocked = status in {"verify_employer", "verify_offer", "intermediary_not_employer", "suspended"}
    contactable = status in {"ready_for_call", "routing_required"}
    label = _role_label(angle.entry_offer.title)
    place = _location_phrase(angle.entry_offer.location)
    has_screen = _catalog_claim(angle, "features.phone_screen")
    has_hunt = _catalog_claim(angle, "features.hunt_campaign_count")
    specialty_safe = bool(angle.specialty_match == "strong_specialty_match" and angle.specialty_label and any(
        c.source_reference == f"specialties:{_norm_label(angle.specialty_label)}" for c in angle.client_safe_facts
    ))
    territory_safe = bool(angle.territorial_relevance == "useful" and any(
        c.source_reference.startswith("territories:") for c in angle.client_safe_facts
    ))
    specialty_line = _specialty_phrase(angle.specialty_label) if specialty_safe else ""
    if territory_safe and specialty_line:
        if place == " dans le Val-de-Marne":
            specialty_line = specialty_line.replace("Je travaille notamment sur", "Je travaille localement sur", 1)
        else:
            specialty_line = specialty_line.removesuffix(".") + " dans le Val-de-Marne."
    territory_line = ("Je travaille localement sur ce secteur." if place == " dans le Val-de-Marne"
                      else "Je travaille notamment sur le Val-de-Marne.") if territory_safe and not specialty_line else ""
    detail = _description_detail(context) if not blocked else None
    intro = (f"Bonjour, {_inline(profile.consultant_name)} de {_inline(profile.network_name)}."
             if profile else "Bonjour, je suis consultant en recrutement.")
    observed = f"J'ai vu votre annonce pour « {label} »{place}."
    credibility = f" {specialty_line}" if specialty_line else ""
    first = f"{observed}{credibility} Vous êtes toujours en recherche sur ce poste ?"
    questions = _spoken_questions(angle)
    questions_after_status = tuple(q for i, q in enumerate(questions) if i != 0)
    questions_after_criteria = tuple(q for i, q in enumerate(questions) if i != 1)
    transition = ("Je vous propose qu'on prenne vingt minutes pour regarder le besoin plus précisément "
                  "et voir si je peux vraiment vous apporter quelque chose.")
    after_response = (f"Si je reprends ce que vous me dites sur « {_inline(confirmed_priority, 140)} », "
                      "je vous propose qu'on prenne vingt minutes pour le cadrer et voir comment je peux vous aider."
                      if confirmed_priority and _inline(confirmed_priority, 140) else None)
    continuation = "Merci. Je voudrais surtout comprendre ce qui compte pour vous sur ce poste."
    gatekeeper_first = (f"Je vous appelle au sujet de votre annonce pour « {label} »{place}. "
                        "Vous sauriez me dire qui s'en occupe chez vous ?")
    gatekeeper_next = ("Je voulais voir avec la personne qui suit ce recrutement "
                       "si une recherche ciblée pourrait lui être utile.")
    phone = (
        PhoneDraft("phone_decision_maker", angle.target_role or "responsable du recrutement", None if blocked else intro,
                   None if blocked else first, None if blocked else continuation, questions_after_status if not blocked else (),
                   None if blocked else transition, contactable, None if blocked else after_response),
        PhoneDraft("phone_director", "dirigeant", None if blocked else intro,
                   None if blocked else f"{observed} Où en êtes-vous sur ce poste ?",
                   None if blocked else "D'accord. Je voudrais comprendre ce qui est prioritaire pour vous.",
                   questions_after_status if not blocked else (), None if blocked else transition, contactable,
                   None if blocked else after_response),
        PhoneDraft("phone_hr", "RH ou recrutement", None if blocked else intro,
                   None if blocked else f"{observed} Une recherche ciblée en complément de votre démarche pourrait-elle être utile ?",
                   None if blocked else "Je voudrais juste situer où vous en êtes aujourd'hui.",
                   questions if not blocked else (), None if blocked else transition, contactable,
                   None if blocked else after_response),
        PhoneDraft("phone_manager", "manager métier", None if blocked else intro,
                   None if blocked else f"{observed} Quel critère métier est le plus important pour vous ?",
                   None if blocked else "Qui valide un éventuel appui extérieur sur ce recrutement ?",
                   questions_after_criteria if not blocked else (), None if blocked else transition, contactable,
                   None if blocked else after_response),
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
    subject_place = ("Le " + place.removeprefix(" au ") if place.startswith(" au ") else
                     place.removeprefix(" à ").removeprefix(" dans le "))
    subject = f"{label} – {subject_place}" if place else f"Recrutement : {label}"
    primary = angle.primary_value_proposition
    if primary == "profile_criteria_clarification" and has_hunt and has_screen:
        proposal = ("Je peux vous aider à cadrer les critères indispensables, "
                    "puis cibler et préqualifier les profils correspondants.")
    elif primary == "profile_criteria_clarification" and has_hunt:
        proposal = "Je peux vous aider à cadrer les critères indispensables avant une recherche ciblée."
    elif primary == "targeted_search" and has_hunt and has_screen:
        proposal = ("Je peux compléter votre recherche par une chasse ciblée "
                    "et une première qualification téléphonique des profils.")
    elif primary == "targeted_search" and has_hunt:
        proposal = "Je peux compléter votre recherche par une chasse ciblée."
    elif primary == "preselection" and has_screen:
        proposal = ("Je peux faire une première qualification téléphonique sur les critères "
                    "que vous souhaitez vérifier avant vos entretiens.")
    elif primary == "territorial_targeting" and territory_safe and has_hunt:
        proposal = "Je peux cibler une recherche sur le périmètre local que vous souhaitez couvrir."
    else:
        proposal = "Je souhaiterais voir si une recherche complémentaire pourrait vous être utile."
    pitch = " ".join(filter(None, (observed, detail[0] if detail else None,
                                   specialty_line, territory_line, proposal)))
    cold = (f"Bonjour,\n\n{pitch}\n\n"
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
        if claim.source_reference == f"{angle.selected_offer_code}.features.phone_screen":
            return has_screen
        if claim.source_reference == f"{angle.selected_offer_code}.features.hunt_campaign_count":
            return has_hunt
        if claim.source_reference.startswith("territories:"):
            return territory_safe
        return bool(angle.specialty_match == "strong_specialty_match" and angle.specialty_label
                    and claim.source_reference == f"specialties:{_norm_label(angle.specialty_label)}")
    claims = tuple(c for c in angle.client_safe_facts if was_used(c)) if not blocked else ()
    if detail and not blocked:
        claims += (detail[1],)
    if profile and not blocked:
        claims += (
            AngleClaim(profile.consultant_name, "consultant_profile_fact", "local_commercial_profile",
                       "consultant_name", "self_declared", True),
            AngleClaim(profile.network_name, "consultant_profile_fact", "local_commercial_profile",
                       "network_name", "self_declared", True),
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
        communication_title=label,
        entry_offer=angle.entry_offer, commercial_angle=angle, phone=phone, email=email,
        priority_objections=_priority_codes(angle) if objections else (), objections=objections,
        evidence=PackEvidence(claims, tuple(dict.fromkeys(c.source_reference for c in claims)),
                              warnings, angle.do_not_claim), internal=internal,
    )


def get_commercial_approach_pack(
    session: Session, company_key: str, department_code: str = "94", now: Optional[datetime] = None,
    selected_offer_code: Optional[str] = None,
) -> Optional[CommercialApproachPack]:
    profile = read_profile(session)
    catalog: tuple[CommercialOffer, ...] = tuple(list_offers(session))
    policy: Optional[CommercialPolicy] = read_policy(session)
    priority = lambda title: int(specialty_for_role(title, profile)[0] == "strong_specialty_match")
    context = get_commercial_approach_context(session, company_key, department_code, now, specialty_priority=priority)
    if context is None:
        return None
    angle = build_commercial_angle(context, profile, catalog, policy, selected_offer_code)
    from sqlalchemy import select
    from app.models import CommercialInteraction
    latest = session.scalar(select(CommercialInteraction).where(
        CommercialInteraction.company_key == company_key,
    ).order_by(CommercialInteraction.happened_at.desc(), CommercialInteraction.id.desc()).limit(1))
    return build_commercial_approach_pack(
        context, angle, profile,
        call_request_kind="email" if latest and latest.outcome == "email_requested" else None,
        confirmed_priority=latest.priority_expressed if latest else None,
    )
