from app.services.company_enrichment.contracts import MatchStatus
from app.services.opportunities.company import CompanyOpportunity, OpportunitySignal
from app.services.opportunities.intermediary import (
    IntermediaryDescriptionEvidence,
    analyze_intermediary_descriptions,
)
from app.services.scoring.company import (
    EmployerRelationshipStatus,
    EnrichmentSnapshot,
    ScoreCategory,
    ScoringPolicy,
    categorize_score,
    score_company_opportunity,
)


def opportunity(
    *,
    name="Example",
    offers=1,
    roles=1,
    newest_age=10,
    over_21=0,
    over_45=0,
    over_90=0,
    sources=("france_travail",),
    recurrent=False,
    multi_location=False,
    description_evidence=None,
):
    signals = tuple(
        OpportunitySignal(name, active, name)
        for name, active in (
            ("recurrent_observation_signal", recurrent),
            ("multi_location_signal", multi_location),
        )
    )
    return CompanyOpportunity(
        company_key=name.casefold(), company_name=name, department_code="94",
        active_offer_count=offers, distinct_job_title_count=roles,
        distinct_source_count=len(sources), sources=sources,
        oldest_offer_created_at="2026-09-01T00:00:00Z" if newest_age is not None else None,
        newest_offer_created_at="2026-09-15T00:00:00Z" if newest_age is not None else None,
        oldest_offer_age_days=max(newest_age or 0, 1) if newest_age is not None else None,
        newest_offer_age_days=newest_age, contract_types=("CDI",), communes=("94028",),
        location_labels=("Créteil",), offer_ids=("source:1",), job_titles=("Role",),
        cdi_offer_count=offers, cdd_offer_count=0, other_contract_offer_count=0,
        offers_over_21_days=over_21, offers_over_45_days=over_45,
        offers_over_90_days=over_90, average_offer_age_days=float(newest_age) if newest_age is not None else None,
        median_offer_age_days=float(newest_age) if newest_age is not None else None, signals=signals,
        intermediary_description_evidence=description_evidence or IntermediaryDescriptionEvidence(),
    )


def enrichment(
    status=MatchStatus.HIGH_CONFIDENCE,
    sector="private",
    confirmed=True,
    employee_range=None,
    naf_code=None,
):
    return EnrichmentSnapshot(
        match_status=status, entity_sector_type=sector,
        has_confirmed_identity=confirmed, has_contactable_location=confirmed,
        employee_range=employee_range, naf_code=naf_code,
    )


def test_private_company_with_recent_multiple_offers_scores_high():
    result = score_company_opportunity(
        opportunity(offers=8, roles=4, newest_age=3, over_21=2, over_45=1, recurrent=True, multi_location=True, sources=("a", "b")),
        enrichment(),
    )
    assert result.total_score >= 75
    assert result.category == ScoreCategory.VERY_HIGH
    assert result.subscores.direct_need <= 35
    assert result.subscores.latent_signals <= 25


def test_old_persistent_offer_increases_latent_score():
    persistent = score_company_opportunity(opportunity(newest_age=60, over_21=1, over_45=1, recurrent=True), enrichment())
    recent = score_company_opportunity(opportunity(newest_age=60), enrichment())
    assert persistent.subscores.latent_signals > recent.subscores.latent_signals


def test_public_entity_is_strongly_penalized_but_not_zeroed():
    demand = opportunity(offers=12, roles=7, newest_age=2, over_21=3, over_45=2, over_90=1, recurrent=True, multi_location=True, sources=("a", "b"))
    public = score_company_opportunity(demand, enrichment(sector="public"))
    private = score_company_opportunity(demand, enrichment(sector="private"))
    assert 0 < public.total_score < private.total_score
    assert any(item.code == "public_entity_penalty" for item in public.penalties)


def test_intermediary_is_commercially_penalized_and_volume_is_capped():
    result = score_company_opportunity(
        opportunity(offers=30, roles=10, newest_age=2, over_21=2, recurrent=True),
        enrichment(status=MatchStatus.GENERIC, sector="private", confirmed=False),
    )
    assert result.subscores.direct_need == 20
    assert any(item.code == "generic_or_intermediary_penalty" for item in result.penalties)


def test_nonprofit_is_treated_more_moderately_than_public():
    demand = opportunity(offers=6, roles=3, newest_age=5, over_21=1)
    nonprofit = score_company_opportunity(demand, enrichment(sector="nonprofit"))
    public = score_company_opportunity(demand, enrichment(sector="public"))
    assert nonprofit.total_score > public.total_score
    assert not nonprofit.penalties


def test_not_found_with_strong_direct_demand_remains_interesting():
    result = score_company_opportunity(
        opportunity(offers=10, roles=6, newest_age=2, over_21=2, over_45=1, recurrent=True, sources=("a", "b")),
        enrichment(status=MatchStatus.NOT_FOUND, sector="unknown", confirmed=False),
    )
    assert result.total_score >= 55
    assert result.category in {ScoreCategory.GOOD, ScoreCategory.VERY_HIGH}


def test_high_confidence_identity_without_need_is_not_artificially_prioritized():
    result = score_company_opportunity(opportunity(offers=1, roles=1, newest_age=90), enrichment())
    assert result.category in {ScoreCategory.LOW, ScoreCategory.MEDIUM}
    assert result.category != ScoreCategory.VERY_HIGH


def test_freshness_decreases_with_offer_age():
    recent = score_company_opportunity(opportunity(newest_age=3), enrichment())
    old = score_company_opportunity(opportunity(newest_age=60), enrichment())
    assert recent.subscores.evidence_freshness > old.subscores.evidence_freshness
    assert recent.total_score > old.total_score


def test_score_is_bounded_between_zero_and_one_hundred():
    high = score_company_opportunity(opportunity(offers=999, roles=999, newest_age=0, over_21=999, over_45=999, over_90=999, recurrent=True, multi_location=True, sources=("a", "b")), enrichment())
    low = score_company_opportunity(opportunity(newest_age=None), enrichment(status=MatchStatus.GENERIC, sector="public", confirmed=False))
    assert 0 <= high.total_score <= 100
    assert 0 <= low.total_score <= 100


def test_categories_are_deterministic_at_explicit_thresholds():
    policy = ScoringPolicy(very_high_threshold=75, good_threshold=55, medium_threshold=35)
    assert categorize_score(75, policy) == ScoreCategory.VERY_HIGH
    assert categorize_score(55, policy) == ScoreCategory.GOOD
    assert categorize_score(35, policy) == ScoreCategory.MEDIUM
    assert categorize_score(34, policy) == ScoreCategory.LOW


def test_human_scale_employee_range_receives_explicit_bonus():
    result = score_company_opportunity(opportunity(), enrichment(employee_range="20-49"))
    assert any(item.code == "employee_range_human_scale_bonus" and item.points == 3 for item in result.commercial_adjustments)


def test_mid_market_employee_range_receives_explicit_bonus():
    result = score_company_opportunity(opportunity(), enrichment(employee_range="100-199"))
    assert any(item.code == "employee_range_mid_market_bonus" and item.points == 1 for item in result.commercial_adjustments)


def test_large_employee_range_receives_explicit_adjustment():
    result = score_company_opportunity(opportunity(), enrichment(employee_range="250+"))
    assert any(item.code == "employee_range_large_employer_adjustment" and item.points == -8 for item in result.commercial_adjustments)


def test_unknown_or_missing_employee_range_has_no_adjustment():
    for employee_range in (None, "unknown", "0", "unrecognised"):
        result = score_company_opportunity(opportunity(), enrichment(employee_range=employee_range))
        assert any(item.code == "employee_range_no_adjustment" and item.points == 0 for item in result.commercial_adjustments)


def test_size_adjustments_keep_total_score_bounded():
    result = score_company_opportunity(
        opportunity(offers=999, roles=999, newest_age=0, over_21=999, over_45=999, over_90=999, recurrent=True, multi_location=True, sources=("a", "b")),
        enrichment(employee_range="20-49"),
    )
    assert result.total_score == 100


def test_public_name_overrides_nonprofit_without_changing_enrichment():
    result = score_company_opportunity(opportunity(name="Ville de Test"), enrichment(sector="nonprofit"))
    assert result.subscores.commercial_relevance == 5
    assert any(item.code == "public_signal:name_ville_de" for item in result.commercial_adjustments)
    assert any(item.code == "public_entity_penalty" for item in result.penalties)


def test_association_name_is_not_a_public_override():
    result = score_company_opportunity(opportunity(name="Association Exemple"), enrichment(sector="nonprofit"))
    assert result.subscores.commercial_relevance == 10
    assert not any(item.code.startswith("public_signal:") for item in result.commercial_adjustments)


def test_ambiguous_name_without_public_marker_is_not_overridden():
    result = score_company_opportunity(opportunity(name="Centre Exemple"), enrichment(sector="nonprofit"))
    assert result.subscores.commercial_relevance == 10
    assert not any(item.code.startswith("public_signal:") for item in result.commercial_adjustments)


def test_recruitment_name_is_treated_as_intermediary():
    result = score_company_opportunity(opportunity(name="NEXUS RECRUTEMENT", offers=30, roles=10), enrichment())
    assert result.subscores.direct_need == 20
    assert result.subscores.commercial_relevance == 3
    assert any(item.code == "intermediary_signal:name_recrutement" for item in result.commercial_adjustments)


def test_confirmed_naf_78_is_treated_as_intermediary():
    result = score_company_opportunity(opportunity(name="Entreprise Neutre", offers=30, roles=10), enrichment(naf_code="78.10Z"))
    assert result.subscores.direct_need == 20
    assert any(item.code == "intermediary_signal:naf_78" for item in result.commercial_adjustments)


def test_explicit_intermediary_aliases_are_treated_as_intermediaries():
    for name, expected_code in (("APPEL MEDICAL", "alias_appel_medical"), ("LE CABRH", "alias_le_cabrh")):
        result = score_company_opportunity(opportunity(name=name, offers=30, roles=10), enrichment())
        assert any(item.code == f"intermediary_signal:{expected_code}" for item in result.commercial_adjustments)


def test_distinctive_alias_matches_as_a_complete_phrase_in_a_longer_name():
    result = score_company_opportunity(
        opportunity(name="RANDSTAD PROFESSIONAL - APPEL MEDICAL - X", offers=30, roles=10),
        enrichment(),
    )
    assert result.employer_relationship_status == EmployerRelationshipStatus.INTERMEDIARY
    assert any(item.code == "intermediary_signal:alias_appel_medical" for item in result.commercial_adjustments)


def test_distinctive_alias_phrase_normalizes_case_and_punctuation():
    result = score_company_opportunity(opportunity(name="Randstad / Appel-Médical (IDF)"), enrichment())
    assert any(item.code == "intermediary_signal:alias_appel_medical" for item in result.commercial_adjustments)


def test_alias_phrase_does_not_match_a_partial_word():
    result = score_company_opportunity(opportunity(name="Appel Medicalement Services"), enrichment())
    assert result.employer_relationship_status == EmployerRelationshipStatus.DIRECT_EMPLOYER


def test_rh_alone_is_not_an_intermediary_signal():
    result = score_company_opportunity(opportunity(name="Cabinet RH", offers=30, roles=10), enrichment())
    assert result.subscores.direct_need == 35
    assert not any(item.code.startswith("intermediary_signal:") for item in result.commercial_adjustments)


def text_evidence(*descriptions):
    return analyze_intermediary_descriptions(tuple(
        type("Offer", (), {
            "source": "source", "source_offer_id": str(index), "title": "Role",
            "location_label": "Créteil", "description": description,
        })()
        for index, description in enumerate(descriptions)
    ))


def test_expertnet_like_text_evidence_is_an_intermediary():
    evidence = text_evidence(*(["Nous recrutons pour l'un de nos clients."] * 6 + ["Besoin direct"] * 4))
    result = score_company_opportunity(opportunity(offers=10, roles=6, description_evidence=evidence), enrichment())
    assert result.employer_relationship_status == EmployerRelationshipStatus.INTERMEDIARY
    assert result.intermediary_description_evidence.strong_signal_offer_count == 6
    assert result.subscores.direct_need == 20


def test_multiple_offers_without_text_signal_stay_direct_employer():
    result = score_company_opportunity(opportunity(offers=10, roles=6), enrichment())
    assert result.employer_relationship_status == EmployerRelationshipStatus.DIRECT_EMPLOYER


def test_one_explicit_client_offer_is_suspected_not_confirmed_intermediary():
    evidence = text_evidence("Nous recrutons pour notre client.")
    result = score_company_opportunity(opportunity(description_evidence=evidence), enrichment())
    assert result.employer_relationship_status == EmployerRelationshipStatus.INTERMEDIARY_SUSPECTED
    assert result.subscores.direct_need == 8


def test_banal_client_word_is_not_a_false_positive():
    evidence = text_evidence("La relation client est au cœur de ce poste. Notre agence vous accompagne.")
    result = score_company_opportunity(opportunity(description_evidence=evidence), enrichment())
    assert result.employer_relationship_status == EmployerRelationshipStatus.DIRECT_EMPLOYER


def test_recruitment_firm_wording_is_a_strong_description_signal():
    evidence = text_evidence("Cabinet de recrutement recrute pour son client.")
    result = score_company_opportunity(opportunity(description_evidence=evidence), enrichment())
    assert result.employer_relationship_status == EmployerRelationshipStatus.INTERMEDIARY_SUSPECTED
    assert "recruitment_firm" in result.intermediary_description_evidence.marker_types


def test_existing_generic_and_naf_structural_signals_remain_intermediaries():
    generic = score_company_opportunity(opportunity(), enrichment(status=MatchStatus.GENERIC, confirmed=False))
    naf = score_company_opportunity(opportunity(), enrichment(naf_code="78.10Z"))
    assert generic.employer_relationship_status == EmployerRelationshipStatus.INTERMEDIARY
    assert naf.employer_relationship_status == EmployerRelationshipStatus.INTERMEDIARY


def test_multi_location_without_intermediation_text_stays_direct_employer():
    result = score_company_opportunity(opportunity(offers=30, roles=10, multi_location=True), enrichment())
    assert result.employer_relationship_status == EmployerRelationshipStatus.DIRECT_EMPLOYER


def test_suspected_intermediary_has_moderate_penalty_without_direct_need_cap():
    evidence = text_evidence("Nous recrutons pour notre client.")
    suspected = score_company_opportunity(opportunity(offers=10, roles=6, description_evidence=evidence), enrichment())
    direct = score_company_opportunity(opportunity(offers=10, roles=6), enrichment())
    assert suspected.total_score == direct.total_score - 6
    assert suspected.subscores.direct_need == direct.subscores.direct_need
    assert any(item.code == "intermediary_suspected_penalty" for item in suspected.penalties)


def test_suspected_intermediary_penalty_is_configurable():
    evidence = text_evidence("Nous recrutons pour notre client.")
    result = score_company_opportunity(
        opportunity(description_evidence=evidence), enrichment(),
        ScoringPolicy(intermediary_suspected_penalty=2),
    )
    assert any(item.code == "intermediary_suspected_penalty" and item.points == -2 for item in result.penalties)


def test_confirmed_intermediary_keeps_existing_commercial_treatment():
    result = score_company_opportunity(opportunity(offers=30, roles=10), enrichment(status=MatchStatus.GENERIC, confirmed=False))
    assert result.subscores.direct_need == 20
    assert result.subscores.commercial_relevance == 3
    assert any(item.code == "generic_or_intermediary_penalty" for item in result.penalties)


def test_description_classification_and_score_are_deterministic():
    evidence = text_evidence("Pour le compte de notre client.", "Pour le compte de notre client.", "Autre offre.")
    first = score_company_opportunity(opportunity(offers=3, description_evidence=evidence), enrichment())
    second = score_company_opportunity(opportunity(offers=3, description_evidence=evidence), enrichment())
    assert first == second
