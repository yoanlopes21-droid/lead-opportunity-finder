"""Pure, deterministic commercial scoring built from local opportunity facts."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import re
from typing import Optional, Sequence
import unicodedata

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import CompanyEnrichment
from app.services.company_enrichment.contracts import MatchStatus
from app.services.opportunities.company import (
    CompanyOpportunity,
    OpportunitySignal,
    aggregate_active_company_opportunities,
)
from app.services.opportunities.intermediary import IntermediaryDescriptionEvidence


class ScoreCategory:
    VERY_HIGH = "🔥 priorité très forte"
    GOOD = "🟢 bon prospect"
    MEDIUM = "🟠 à surveiller / priorité moyenne"
    LOW = "⚪ faible priorité"


@dataclass(frozen=True)
class ScoringPolicy:
    """All V1 weights and category thresholds in one easily adjustable place."""

    very_high_threshold: int = 75
    good_threshold: int = 55
    medium_threshold: int = 35
    public_entity_penalty: int = 20
    intermediary_penalty: int = 15
    intermediary_direct_need_cap: int = 20
    intermediary_suspected_penalty: int = 6
    intermediary_text_minimum_offers: int = 3
    intermediary_text_minimum_proportion: float = 0.30

    def __post_init__(self) -> None:
        if not 0 <= self.medium_threshold <= self.good_threshold <= self.very_high_threshold <= 100:
            raise ValueError("score category thresholds must be ordered between 0 and 100")
        if min(self.public_entity_penalty, self.intermediary_penalty, self.intermediary_suspected_penalty) < 0:
            raise ValueError("commercial penalties must not be negative")
        if not 0 <= self.intermediary_direct_need_cap <= 35:
            raise ValueError("intermediary direct need cap must be between 0 and 35")
        if self.intermediary_text_minimum_offers < 2:
            raise ValueError("intermediary text minimum offers must be at least 2")
        if not 0 < self.intermediary_text_minimum_proportion <= 1:
            raise ValueError("intermediary text minimum proportion must be between 0 and 1")


@dataclass(frozen=True)
class EnrichmentSnapshot:
    """Minimal provider-neutral enrichment information consumed by scoring."""

    match_status: str = MatchStatus.NOT_FOUND
    entity_sector_type: str = "unknown"
    has_confirmed_identity: bool = False
    has_contactable_location: bool = False
    employee_range: Optional[str] = None
    naf_code: Optional[str] = None

    @classmethod
    def from_model(cls, enrichment: Optional[CompanyEnrichment]) -> "EnrichmentSnapshot":
        if enrichment is None:
            return cls()
        return cls(
            match_status=enrichment.match_status,
            entity_sector_type=enrichment.entity_sector_type or "unknown",
            has_confirmed_identity=bool(enrichment.siren and enrichment.official_name),
            has_contactable_location=bool(enrichment.address or enrichment.postal_code or enrichment.commune),
            employee_range=enrichment.employee_range,
            naf_code=enrichment.naf_code,
        )


@dataclass(frozen=True)
class ScoreReason:
    code: str
    message: str
    points: int


@dataclass(frozen=True)
class ScoreSubscores:
    direct_need: int
    latent_signals: int
    commercial_relevance: int
    accessibility: int
    evidence_freshness: int

    @property
    def total_before_penalties(self) -> int:
        return sum((
            self.direct_need,
            self.latent_signals,
            self.commercial_relevance,
            self.accessibility,
            self.evidence_freshness,
        ))


@dataclass(frozen=True)
class CompanyScoringResult:
    company_key: str
    company_name: str
    department_code: str
    total_score: int
    category: str
    subscores: ScoreSubscores
    positive_reasons: tuple[ScoreReason, ...]
    commercial_adjustments: tuple[ScoreReason, ...]
    penalties: tuple[ScoreReason, ...]
    signals_used: tuple[OpportunitySignal, ...]
    employer_relationship_status: str
    employer_relationship_reasons: tuple[ScoreReason, ...]
    intermediary_description_evidence: IntermediaryDescriptionEvidence


class EmployerRelationshipStatus:
    DIRECT_EMPLOYER = "direct_employer"
    INTERMEDIARY_SUSPECTED = "intermediary_suspected"
    INTERMEDIARY = "intermediary"


@dataclass(frozen=True)
class EmployerRelationshipAssessment:
    status: str
    reasons: tuple[ScoreReason, ...]
    description_evidence: IntermediaryDescriptionEvidence


def score_company_opportunity(
    opportunity: CompanyOpportunity,
    enrichment: Optional[EnrichmentSnapshot] = None,
    policy: ScoringPolicy = ScoringPolicy(),
) -> CompanyScoringResult:
    """Score one aggregated company without querying or persisting anything."""
    enrichment = enrichment or EnrichmentSnapshot()
    positive_reasons: list[ScoreReason] = []
    commercial_adjustments: list[ScoreReason] = []
    penalties: list[ScoreReason] = []
    active_signals = tuple(signal for signal in opportunity.signals if signal.active)
    relationship = classify_employer_relationship(opportunity, enrichment, policy)
    is_intermediary = relationship.status == EmployerRelationshipStatus.INTERMEDIARY
    is_intermediary_suspected = relationship.status == EmployerRelationshipStatus.INTERMEDIARY_SUSPECTED
    public_signal = _public_signal(opportunity.company_name, enrichment)

    direct_need = _direct_need_score(opportunity, positive_reasons)
    if is_intermediary and direct_need > policy.intermediary_direct_need_cap:
        direct_need = policy.intermediary_direct_need_cap
        commercial_adjustments.append(ScoreReason(
            "intermediary_direct_need_cap",
            "Le besoin direct est plafonné pour un intermédiaire identifié.",
            0,
        ))

    latent_signals = _latent_signal_score(opportunity, positive_reasons)
    commercial_relevance = _commercial_relevance_score(
        enrichment, public_signal is not None, is_intermediary, positive_reasons
    )
    accessibility = _accessibility_score(opportunity, enrichment, positive_reasons)
    evidence_freshness = _evidence_freshness_score(opportunity, positive_reasons)

    if public_signal:
        commercial_adjustments.append(ScoreReason(
            f"public_signal:{public_signal}",
            "Signal commercial public détecté sans modifier l'enrichissement source.",
            0,
        ))
        penalties.append(ScoreReason(
            "public_entity_penalty",
            "Entité publique : adressabilité commerciale fortement réduite.",
            -policy.public_entity_penalty,
        ))
    commercial_adjustments.extend(relationship.reasons)
    if is_intermediary:
        penalties.append(ScoreReason(
            "generic_or_intermediary_penalty",
            "Intermédiaire ou recruteur identifié : faible probabilité de prospect client direct.",
            -policy.intermediary_penalty,
        ))
    elif is_intermediary_suspected:
        penalties.append(ScoreReason(
            "intermediary_suspected_penalty",
            "Intermédiaire ou diffuseur possible : vérification recommandée avant prospection.",
            -policy.intermediary_suspected_penalty,
        ))
    commercial_adjustments.append(_employee_range_adjustment(enrichment.employee_range))

    subscores = ScoreSubscores(
        direct_need=direct_need,
        latent_signals=latent_signals,
        commercial_relevance=commercial_relevance,
        accessibility=accessibility,
        evidence_freshness=evidence_freshness,
    )
    total_score = max(0, min(
        100,
        subscores.total_before_penalties
        + sum(item.points for item in commercial_adjustments)
        + sum(item.points for item in penalties),
    ))
    return CompanyScoringResult(
        company_key=opportunity.company_key,
        company_name=opportunity.company_name,
        department_code=opportunity.department_code,
        total_score=total_score,
        category=categorize_score(total_score, policy),
        subscores=subscores,
        positive_reasons=tuple(positive_reasons),
        commercial_adjustments=tuple(commercial_adjustments),
        penalties=tuple(penalties),
        signals_used=active_signals,
        employer_relationship_status=relationship.status,
        employer_relationship_reasons=relationship.reasons,
        intermediary_description_evidence=relationship.description_evidence,
    )


def categorize_score(score: int, policy: ScoringPolicy = ScoringPolicy()) -> str:
    """Map an already bounded score to a stable, configurable V1 category."""
    bounded = max(0, min(100, score))
    if bounded >= policy.very_high_threshold:
        return ScoreCategory.VERY_HIGH
    if bounded >= policy.good_threshold:
        return ScoreCategory.GOOD
    if bounded >= policy.medium_threshold:
        return ScoreCategory.MEDIUM
    return ScoreCategory.LOW


def score_active_company_opportunities(
    session: Session,
    department_code: str = "94",
    provider: str = "dinum",
    now: Optional[datetime] = None,
    policy: ScoringPolicy = ScoringPolicy(),
) -> tuple[CompanyScoringResult, ...]:
    """Recalculate local scores for any department without persisting assessments."""
    aggregation = aggregate_active_company_opportunities(
        session, department_code=department_code, now=now
    )
    enrichments = {
        item.company_key: EnrichmentSnapshot.from_model(item)
        for item in session.scalars(
            select(CompanyEnrichment).where(CompanyEnrichment.provider == provider)
        )
    }
    scored = (
        score_company_opportunity(item, enrichments.get(item.company_key), policy)
        for item in aggregation.opportunities
    )
    return tuple(sorted(scored, key=lambda item: (-item.total_score, item.company_name.casefold(), item.company_key)))


def _direct_need_score(opportunity: CompanyOpportunity, reasons: list[ScoreReason]) -> int:
    volume_points = _tiered_points(opportunity.active_offer_count, ((10, 24), (5, 19), (2, 13), (1, 6)))
    _add_if_positive(reasons, "active_offer_volume", f"{opportunity.active_offer_count} offre(s) active(s).", volume_points)
    diversity_points = min(max(opportunity.distinct_job_title_count - 1, 0), 6)
    _add_if_positive(reasons, "role_diversity", f"{opportunity.distinct_job_title_count} poste(s) distinct(s).", diversity_points)
    simultaneous_points = 3 if opportunity.active_offer_count >= 2 else 0
    _add_if_positive(reasons, "simultaneous_needs", "Plusieurs besoins actifs simultanés.", simultaneous_points)
    recent_points = 2 if _age_at_most(opportunity.newest_offer_age_days, 21) else 0
    _add_if_positive(reasons, "recent_active_need", "Au moins une offre active récente (21 jours ou moins).", recent_points)
    return min(35, volume_points + diversity_points + simultaneous_points + recent_points)


def _latent_signal_score(opportunity: CompanyOpportunity, reasons: list[ScoreReason]) -> int:
    points = 0
    for minimum_age, awarded, code, message in (
        (21, 6, "persistent_over_21_days", "Offres actives persistantes depuis plus de 21 jours."),
        (45, 6, "persistent_over_45_days", "Offres actives persistantes depuis plus de 45 jours."),
        (90, 5, "persistent_over_90_days", "Offres actives persistantes depuis plus de 90 jours."),
    ):
        count = {21: opportunity.offers_over_21_days, 45: opportunity.offers_over_45_days, 90: opportunity.offers_over_90_days}[minimum_age]
        if count:
            points += awarded
            reasons.append(ScoreReason(code, message, awarded))
    if _has_signal(opportunity, "recurrent_observation_signal"):
        points += 5
        reasons.append(ScoreReason("recurrent_observation", "Besoin observé dans plusieurs collectes.", 5))
    if _has_signal(opportunity, "multi_location_signal"):
        points += 3
        reasons.append(ScoreReason("multi_location_need", "Besoins répartis sur plusieurs lieux.", 3))
    return min(25, points)


def _commercial_relevance_score(
    enrichment: EnrichmentSnapshot,
    is_effectively_public: bool,
    is_intermediary: bool,
    reasons: list[ScoreReason],
) -> int:
    points_by_sector = {"private": 20, "unknown": 12, "nonprofit": 10, "public": 5}
    effective_sector = "public" if is_effectively_public else enrichment.entity_sector_type
    points = points_by_sector.get(effective_sector, 12)
    if is_intermediary:
        points = 3
        message = "Intermédiaire identifié : pertinence commerciale limitée."
    else:
        message = {
            "private": "Entité privée commercialement adressable.",
            "nonprofit": "Association : pertinence distincte du secteur public.",
            "public": "Entité publique conservée comme besoin potentiellement exceptionnel.",
        }.get(effective_sector, "Nature juridique non confirmée : aucun rejet automatique.")
    _add_if_positive(reasons, "commercial_relevance", message, points)
    return points


def _accessibility_score(
    opportunity: CompanyOpportunity,
    enrichment: EnrichmentSnapshot,
    reasons: list[ScoreReason],
) -> int:
    points_by_status = {
        MatchStatus.HIGH_CONFIDENCE: 7,
        MatchStatus.REVIEW_NEEDED: 5,
        MatchStatus.AMBIGUOUS: 3,
        MatchStatus.NOT_FOUND: 2,
        MatchStatus.GENERIC: 1,
        MatchStatus.ERROR: 1,
    }
    points = points_by_status.get(enrichment.match_status, 2)
    _add_if_positive(reasons, "identity_accessibility", "Informations d'identité disponibles sans bloquer les cas incertains.", points)
    if enrichment.has_confirmed_identity:
        points += 1
        reasons.append(ScoreReason("confirmed_legal_identity", "Identité juridique confirmée.", 1))
    if enrichment.has_contactable_location or opportunity.communes or opportunity.location_labels:
        points += 1
        reasons.append(ScoreReason("location_context", "Contexte de localisation disponible pour vérification.", 1))
    if opportunity.distinct_source_count >= 2:
        points += 1
        reasons.append(ScoreReason("multiple_sources", "Plusieurs sources renforcent la vérifiabilité.", 1))
    return min(10, points)


def _evidence_freshness_score(opportunity: CompanyOpportunity, reasons: list[ScoreReason]) -> int:
    newest = opportunity.newest_offer_age_days
    if _age_at_most(newest, 7):
        points = 6
        reasons.append(ScoreReason("very_recent_evidence", "Offre la plus récente publiée depuis 7 jours ou moins.", 6))
    elif _age_at_most(newest, 21):
        points = 4
        reasons.append(ScoreReason("recent_evidence", "Offre la plus récente publiée depuis 21 jours ou moins.", 4))
    elif _age_at_most(newest, 45):
        points = 2
        reasons.append(ScoreReason("aging_evidence", "Offre la plus récente encore relativement récente.", 2))
    else:
        points = 0
    if opportunity.distinct_source_count >= 2:
        points += 2
        reasons.append(ScoreReason("corroborated_sources", "Preuves issues de plusieurs sources.", 2))
    if _has_signal(opportunity, "recurrent_observation_signal"):
        points += 1
        reasons.append(ScoreReason("repeated_observation_evidence", "Offres confirmées sur plusieurs observations.", 1))
    if opportunity.oldest_offer_created_at and opportunity.newest_offer_created_at:
        points += 1
        reasons.append(ScoreReason("dated_evidence", "Dates de publication disponibles pour les preuves.", 1))
    return min(10, points)


def _tiered_points(value: int, tiers: Sequence[tuple[int, int]]) -> int:
    return next((points for threshold, points in tiers if value >= threshold), 0)


def _has_signal(opportunity: CompanyOpportunity, name: str) -> bool:
    return any(signal.name == name and signal.active for signal in opportunity.signals)


def _age_at_most(age_days: Optional[int], limit: int) -> bool:
    return age_days is not None and age_days <= limit


def _add_if_positive(reasons: list[ScoreReason], code: str, message: str, points: int) -> None:
    if points:
        reasons.append(ScoreReason(code, message, points))


_SMALL_EMPLOYEE_RANGES = {"1-2", "3-5", "6-9", "10-19", "20-49"}
_MEDIUM_EMPLOYEE_RANGES = {"50-99", "100-199", "200-249"}
# Only aliases whose complete phrase is distinctive enough may match within a
# longer publishing name. Any future short or ambiguous alias stays exact-only.
_INTERMEDIARY_ALIASES = {"appel medical", "le cabrh"}
_INTERMEDIARY_ALIASES_PHRASE_MATCH = {"appel medical", "le cabrh"}


def _employee_range_adjustment(employee_range: Optional[str]) -> ScoreReason:
    if employee_range in _SMALL_EMPLOYEE_RANGES:
        return ScoreReason(
            "employee_range_human_scale_bonus",
            f"Tranche d'effectif connue {employee_range} : préférence entreprise à taille humaine.",
            3,
        )
    if employee_range in _MEDIUM_EMPLOYEE_RANGES:
        return ScoreReason(
            "employee_range_mid_market_bonus",
            f"Tranche d'effectif connue {employee_range} : pertinence commerciale renforcée.",
            1,
        )
    if employee_range == "250+":
        return ScoreReason(
            "employee_range_large_employer_adjustment",
            "Tranche d'effectif connue 250+ : priorité commerciale réduite sans exclusion.",
            -8,
        )
    return ScoreReason(
        "employee_range_no_adjustment",
        "Tranche d'effectif absente, inconnue ou non exploitable : aucun ajustement.",
        0,
    )


def _public_signal(company_name: str, enrichment: EnrichmentSnapshot) -> Optional[str]:
    if enrichment.entity_sector_type == "public":
        return "entity_sector_type"
    normalized = _commercial_name_key(company_name)
    for prefix, code in (
        ("ville de ", "name_ville_de"),
        ("commune de ", "name_commune_de"),
        ("conseil departemental", "name_conseil_departemental"),
        ("conseil regional", "name_conseil_regional"),
        ("departement de ", "name_departement_de"),
        ("region ", "name_region"),
        ("centre communal d action sociale", "name_ccas_full"),
    ):
        if normalized.startswith(prefix):
            return code
    if _contains_commercial_phrase(normalized, "mairie"):
        return "name_mairie"
    if _contains_commercial_phrase(normalized, "ccas"):
        return "name_ccas"
    return None


def classify_employer_relationship(
    opportunity: CompanyOpportunity,
    enrichment: EnrichmentSnapshot,
    policy: ScoringPolicy = ScoringPolicy(),
) -> EmployerRelationshipAssessment:
    """Classify commercial relationship using structural facts before text evidence."""
    structural_signal = _intermediary_signal(opportunity.company_name, enrichment)
    evidence = opportunity.intermediary_description_evidence
    if structural_signal:
        return EmployerRelationshipAssessment(
            status=EmployerRelationshipStatus.INTERMEDIARY,
            reasons=(ScoreReason(
                f"intermediary_signal:{structural_signal}",
                "Signal commercial d'intermédiaire détecté sans modifier l'enrichissement source.",
                0,
            ),),
            description_evidence=evidence,
        )
    if (
        evidence.strong_signal_offer_count >= policy.intermediary_text_minimum_offers
        and evidence.strong_signal_proportion >= policy.intermediary_text_minimum_proportion
    ):
        return EmployerRelationshipAssessment(
            status=EmployerRelationshipStatus.INTERMEDIARY,
            reasons=_description_reasons(evidence, confirmed=True),
            description_evidence=evidence,
        )
    if evidence.strong_signal_offer_count:
        return EmployerRelationshipAssessment(
            status=EmployerRelationshipStatus.INTERMEDIARY_SUSPECTED,
            reasons=_description_reasons(evidence, confirmed=False),
            description_evidence=evidence,
        )
    return EmployerRelationshipAssessment(
        status=EmployerRelationshipStatus.DIRECT_EMPLOYER,
        reasons=(),
        description_evidence=evidence,
    )


def _description_reasons(
    evidence: IntermediaryDescriptionEvidence, confirmed: bool
) -> tuple[ScoreReason, ...]:
    marker_labels = ", ".join(evidence.marker_types)
    count = evidence.strong_signal_offer_count
    proportion = round(evidence.strong_signal_proportion * 100)
    status_message = (
        "Proportion significative de formulations explicites d'intermédiation."
        if confirmed else "Formulation explicite d'intermédiation détectée : vérification recommandée."
    )
    return (
        ScoreReason(
            "intermediary_description_evidence",
            f"{count} offre(s) sur {evidence.total_offer_count} ({proportion} %) : {status_message}",
            0,
        ),
        ScoreReason(
            "intermediary_description_markers",
            f"Marqueurs textuels explicites : {marker_labels}.",
            0,
        ),
    )


def _intermediary_signal(company_name: str, enrichment: EnrichmentSnapshot) -> Optional[str]:
    if enrichment.match_status == MatchStatus.GENERIC:
        return "generic_or_intermediary"
    if enrichment.has_confirmed_identity and _is_naf_division_78(enrichment.naf_code):
        return "naf_78"
    normalized = _commercial_name_key(company_name)
    if normalized in _INTERMEDIARY_ALIASES:
        return f"alias_{normalized.replace(' ', '_')}"
    for alias in sorted(_INTERMEDIARY_ALIASES_PHRASE_MATCH, key=len, reverse=True):
        if _contains_commercial_phrase(normalized, alias):
            return f"alias_{alias.replace(' ', '_')}"
    for marker in ("recrutement", "interim", "staffing"):
        if _contains_commercial_phrase(normalized, marker):
            return f"name_{marker}"
    return None


def _is_naf_division_78(naf_code: Optional[str]) -> bool:
    return bool(naf_code and re.match(r"^78(?:[.\d]|$)", naf_code.strip()))


def _commercial_name_key(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value).casefold()
    without_accents = "".join(char for char in normalized if not unicodedata.combining(char))
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s]", " ", without_accents)).strip()


def _contains_commercial_phrase(name: str, phrase: str) -> bool:
    return f" {phrase} " in f" {name} "
