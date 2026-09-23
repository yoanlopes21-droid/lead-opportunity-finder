export type OpportunitySignal = { name: string; active: boolean; explanation: string }
export type ScoreReason = { code: string; message: string; points: number }
export type ScoreSubscores = { direct_need: number; latent_signals: number; commercial_relevance: number; accessibility: number; evidence_freshness: number }

export type Provenance = { id: number; provider: string; source_url: string | null; source_type: string; observed_at: string; short_excerpt: string | null; confidence: string | null; reason: string | null }
export type ContactPoint = { id: number; type: string; value: string; scope: string; local_key: string | null; confidence: string; verification_status: string; provider: string | null; person_contact_id: number | null; observed_at: string | null; last_verified_at: string | null; stale: boolean; evidence: Provenance[]; warnings: string[]; commercial_relevance: string }
export type PersonContact = { id: number; display_name: string; relevance: string; role_title: string | null; scope: string; local_key: string | null; confidence: string; verification_status: string; contact_point_ids: number[]; provenance: Provenance[]; warnings: string[] }
export type ContactStrategy = { target_type: string; preferred_channel: string; preferred_contact_point_id: number | null; preferred_person_contact_id: number | null; fallback_channels: string[]; confidence: string; rationale_codes: string[]; short_context: string; warnings: string[]; missing_information: string[]; evidence: Provenance[]; scope: string; local_key: string | null; channel_relevance: string }
export type LocalOpportunity = { local_key: string; commune: string | null; location_label: string | null; department: string; active_offer_count: number; role_diversity: number; representative_roles: string[]; newest_offer_date: string | null; oldest_offer_date: string | null; source_offer_ids: string[]; source_urls: string[]; signals: OpportunitySignal[]; contact_point_ids: number[]; person_contact_ids: number[] }
export type JobOfferEvidence = { source: string; source_offer_id: string; source_url: string | null; discovery_provider: string | null }
export type ActiveJobOffer = { offer_id: string; title: string; commune: string | null; location_label: string | null; display_location: string | null; published_at: string | null; updated_at: string | null; contract_type: string | null; salary: string | null; source: string; source_url: string | null; sources: string[]; source_urls: string[]; source_offer_ids: string[]; evidence: JobOfferEvidence[]; local_key: string; age_days: number | null }
export type OfficialWeb = { verified_site_status: string | null; verified_domain: string | null; verification_score: number; provider: string | null; warnings: string[] }
export type ContactabilitySummary = { scope: string; official_web: OfficialWeb; warnings: string[] }
export type ExclusionType = 'current_client' | 'recent_prospect' | 'manual_exclusion'
export type CommercialExclusionDetails = {
  id: number; company_key: string; siren: string | null; company_name_snapshot: string
  exclusion_type: ExclusionType; reason: string | null; starts_at: string; expires_at: string | null
  created_at: string | null
}

export type CommercialLead = {
  company_key: string; company_name: string; official_name: string | null; siren: string | null; siret: string | null; entity_sector_type: string; employee_range: string | null; department: string; primary_location: string | null
  active_offer_count: number; role_diversity: number; representative_roles: string[]; newest_offer_date: string | null; latent_signals: OpportunitySignal[]
  total_score: number; category: string; subscores: ScoreSubscores; adjustments: ScoreReason[]; positive_reasons: ScoreReason[]; penalties: ScoreReason[]; signals_used: OpportunitySignal[]
  local_opportunities: LocalOpportunity[]; active_job_offers: ActiveJobOffer[]; contacts: ContactPoint[]; people: PersonContact[]; contact_strategy: ContactStrategy; contactability_summary: ContactabilitySummary
  employer_relationship_status: string; employer_relationship_reasons: ScoreReason[]
  is_eligible: boolean; exclusion: CommercialExclusionDetails | null
  commercial_relationship: CommercialRelationship | null
}

export type CommercialLeadPage = { items: CommercialLead[]; total: number; limit: number; offset: number }
export type BraveUsage = { monthly_budget: number; monthly_used: number; monthly_remaining: number; estimated_cost_used_usd: number; estimated_credit_remaining_usd: number; current_period_end: string; days_remaining_in_period: number; status: string }

export type SearchRunStatus = 'queued' | 'running' | 'stopping' | 'stopped' | 'completed' | 'failed'
export type SearchRun = {
  id: number
  status: SearchRunStatus
  department: string
  requested_actionable_leads: number
  current_actionable_leads: number
  candidates_considered: number
  candidates_enriched: number
  brave_requests_used: number
  brave_hard_cap: number
  current_company_key: string | null
  current_company_name: string | null
  current_step: string | null
  completion_reason: string | null
  created_at: string
  started_at: string | null
  finished_at: string | null
  error_summary: string | null
  stop_requested?: boolean
  configuration_fingerprint?: string | null
}
export type SearchRunCreate = { department: '94'; requested_actionable_leads: number; brave_hard_cap: number }

export type JobOfferRefreshRunStatus = 'queued' | 'running' | 'completed' | 'failed'
export type JobOfferRefreshRun = {
  id: number; status: JobOfferRefreshRunStatus; started_at: string; finished_at: string | null
  offers_received: number; offers_new: number; offers_updated: number; offers_unchanged: number
  offers_skipped: number; offers_deactivated: number; temporal_windows: number; pages_processed: number
  active_offer_count: number | null; active_opportunity_count: number | null; error_summary: string | null
}

export type JobSourceBoard = {
  id: number; provider_id: 'greenhouse' | 'lever'; display_name: string; board_identifier: string
  company_name_hint: string; enabled: boolean; created_at: string; updated_at: string
  last_refresh_at: string | null; last_refresh_status: JobOfferRefreshRunStatus | null
  last_error: string | null; last_run_id: number | null; active_offer_count: number
  last_offers_received: number; last_offers_new: number; last_offers_updated: number; last_offers_deactivated: number
  last_duration_seconds: number | null
}
export type JobSourceBoardCreate = {
  provider_id: 'greenhouse' | 'lever'; display_name: string; board_identifier: string
  company_name_hint: string; enabled: boolean
}
export type SourceRefreshRun = {
  id: number; source: string; scope_type: string; scope_value: string; status: JobOfferRefreshRunStatus
  started_at: string; finished_at: string | null; offers_received: number; offers_new: number
  offers_updated: number; offers_unchanged: number; offers_skipped: number; offers_deactivated: number
  pages_processed: number; signals_found: number; signals_promoted: number; brave_requests_used: number
  target_signal_count: number | null; brave_hard_cap: number | null; stop_requested: boolean
  completion_reason: string | null; error_summary: string | null
}
export type RecruitmentSignal = {
  id: number; discovery_provider: string; source: string; source_url: string; domain: string | null
  page_type: 'individual_job_offer' | 'search_or_listing' | 'career_board' | 'unknown'; page_type_label: string
  title: string | null; snippet: string | null; company_name: string | null; job_title: string | null
  location_label: string | null; commune: string | null; department_code: string | null
  published_at: string | null; confidence: number | null; detection_reason: string | null
  extraction: Record<string, unknown>; status: 'new' | 'review_needed' | 'promoted' | 'dismissed'
  promoted_offer_id: number | null; first_seen_at: string; last_seen_at: string; observation_count: number
  is_promotable: boolean; promotion_blockers: string[]
}
export type RecruitmentSignalPage = {
  items: RecruitmentSignal[]; total: number; new_count: number; review_needed_count: number
}

export type CommercialExclusion = CommercialExclusionDetails & {
  active: boolean; status: 'active' | 'expired'; matching_basis: 'siren' | 'company_key'
}
export type CommercialExclusionPage = { items: CommercialExclusion[]; total: number }
export type CommercialExclusionCreate = {
  company_name: string; exclusion_type: ExclusionType; siren?: string; reason?: string
  starts_at?: string; expires_at?: string
}
export type ExclusionImportRow = {
  line_number: number; company_name: string | null; exclusion_type: string | null; siren: string | null
  reason: string | null; starts_at: string | null; expires_at: string | null
  valid: boolean; duplicate: boolean; errors: string[]
}
export type ExclusionImportPreview = {
  rows: ExclusionImportRow[]; valid_count: number; duplicate_count: number; invalid_count: number
}
export type ExclusionImportReport = ExclusionImportPreview & { added_count: number; ignored_count: number }

export type RelationshipStatus = 'contacted' | 'awaiting_reply' | 'follow_up' | 'interested' | 'meeting_scheduled' | 'proposal_sent' | 'client' | 'no_current_need' | 'refused' | 'wrong_contact' | 'do_not_contact'
export type CommercialRelationship = {
  id: number; company_key: string; siren: string | null; company_name_snapshot: string
  status: RelationshipStatus; last_contact_at: string | null; next_action_at: string | null
  note: string | null; outcome: string | null; contact_point_id: number | null; person_contact_id: number | null
  used_channel: string | null; hard_exclusion_id: number | null; is_active: boolean
  created_at: string; updated_at: string; follow_up_timing: 'overdue' | 'today' | 'upcoming' | null
}
export type CommercialRelationshipInput = {
  status: RelationshipStatus; last_contact_at?: string; next_action_at?: string
  note?: string; outcome?: string; contact_point_id?: number; person_contact_id?: number; used_channel?: string
}
export type CommercialRelationshipPage = { items: CommercialRelationship[]; total: number }
