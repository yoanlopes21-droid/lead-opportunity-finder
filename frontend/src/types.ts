export type OpportunitySignal = { name: string; active: boolean; explanation: string }
export type ScoreReason = { code: string; message: string; points: number }
export type ScoreSubscores = { direct_need: number; latent_signals: number; commercial_relevance: number; accessibility: number; evidence_freshness: number }

export type Provenance = { id: number; provider: string; source_url: string | null; source_type: string; observed_at: string; short_excerpt: string | null; confidence: string | null; reason: string | null }
export type ContactPoint = { id: number; type: string; value: string; scope: string; local_key: string | null; confidence: string; verification_status: string; provider: string | null; person_contact_id: number | null; observed_at: string | null; last_verified_at: string | null; stale: boolean; evidence: Provenance[]; warnings: string[]; commercial_relevance: string }
export type PersonContact = { id: number; display_name: string; relevance: string; role_title: string | null; scope: string; local_key: string | null; confidence: string; verification_status: string; contact_point_ids: number[]; provenance: Provenance[]; warnings: string[] }
export type ContactStrategy = { target_type: string; preferred_channel: string; preferred_contact_point_id: number | null; preferred_person_contact_id: number | null; fallback_channels: string[]; confidence: string; rationale_codes: string[]; short_context: string; warnings: string[]; missing_information: string[]; evidence: Provenance[]; scope: string; local_key: string | null; channel_relevance: string }
export type LocalOpportunity = { local_key: string; commune: string | null; location_label: string | null; department: string; active_offer_count: number; role_diversity: number; representative_roles: string[]; newest_offer_date: string | null; oldest_offer_date: string | null; source_offer_ids: string[]; source_urls: string[]; signals: OpportunitySignal[]; contact_point_ids: number[]; person_contact_ids: number[] }
export type ActiveJobOffer = { offer_id: string; title: string; commune: string | null; location_label: string | null; display_location: string | null; published_at: string | null; updated_at: string | null; contract_type: string | null; salary: string | null; source: string; source_url: string | null; local_key: string; age_days: number | null }
export type OfficialWeb = { verified_site_status: string | null; verified_domain: string | null; verification_score: number; provider: string | null; warnings: string[] }
export type ContactabilitySummary = { scope: string; official_web: OfficialWeb; warnings: string[] }

export type CommercialLead = {
  company_key: string; company_name: string; official_name: string | null; entity_sector_type: string; employee_range: string | null; department: string; primary_location: string | null
  active_offer_count: number; role_diversity: number; representative_roles: string[]; newest_offer_date: string | null; latent_signals: OpportunitySignal[]
  total_score: number; category: string; subscores: ScoreSubscores; adjustments: ScoreReason[]; positive_reasons: ScoreReason[]; penalties: ScoreReason[]; signals_used: OpportunitySignal[]
  local_opportunities: LocalOpportunity[]; active_job_offers: ActiveJobOffer[]; contacts: ContactPoint[]; people: PersonContact[]; contact_strategy: ContactStrategy; contactability_summary: ContactabilitySummary
  employer_relationship_status: string; employer_relationship_reasons: ScoreReason[]
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
