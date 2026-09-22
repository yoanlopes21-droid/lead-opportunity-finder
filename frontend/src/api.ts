import type { BraveUsage, CommercialExclusion, CommercialExclusionCreate, CommercialExclusionPage, CommercialLeadPage, CommercialRelationship, CommercialRelationshipInput, CommercialRelationshipPage, ExclusionImportPreview, ExclusionImportReport, JobOfferRefreshRun, SearchRun, SearchRunCreate } from './types'

export const API_BASE_URL = 'http://127.0.0.1:8000'

async function readJson<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${API_BASE_URL}${path}`, init)
  if (!response.ok) {
    let detail: string | undefined
    try {
      const body = await response.json() as { detail?: string | Array<{ msg?: string }> }
      detail = typeof body.detail === 'string' ? body.detail : body.detail?.map((item) => item.msg).filter(Boolean).join(' · ')
    } catch { /* The status remains the useful fallback when a body is not JSON. */ }
    throw new Error(detail || `L’API locale a répondu avec une erreur (${response.status}).`)
  }
  return response.json() as Promise<T>
}

export function fetchCommercialLeads(offset: number, includeExcluded = false): Promise<CommercialLeadPage> {
  const params = new URLSearchParams({ department: '94', include_excluded: String(includeExcluded), limit: '50', offset: String(offset) })
  return readJson<CommercialLeadPage>(`/api/v1/commercial-leads?${params}`)
}

export function fetchCommercialExclusions(filters: { type?: string; status?: string; search?: string } = {}): Promise<CommercialExclusionPage> {
  const params = new URLSearchParams()
  if (filters.type) params.set('type', filters.type)
  if (filters.status && filters.status !== 'all') params.set('status', filters.status)
  if (filters.search?.trim()) params.set('search', filters.search.trim())
  const query = params.toString()
  return readJson<CommercialExclusionPage>(`/api/v1/commercial-exclusions${query ? `?${query}` : ''}`)
}

export function createCommercialExclusion(input: CommercialExclusionCreate): Promise<CommercialExclusion> {
  return readJson<CommercialExclusion>('/api/v1/commercial-exclusions', {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(input),
  })
}

export async function deleteCommercialExclusion(id: number): Promise<void> {
  const response = await fetch(`${API_BASE_URL}/api/v1/commercial-exclusions/${id}`, { method: 'DELETE' })
  if (!response.ok) {
    let detail = `L’API locale a répondu avec une erreur (${response.status}).`
    try { detail = (await response.json() as { detail?: string }).detail || detail } catch { /* Keep fallback. */ }
    throw new Error(detail)
  }
}

export function previewCommercialExclusionCsv(content: string): Promise<ExclusionImportPreview> {
  return readJson<ExclusionImportPreview>('/api/v1/commercial-exclusions/import/preview', {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ content }),
  })
}

export function importCommercialExclusionCsv(content: string): Promise<ExclusionImportReport> {
  return readJson<ExclusionImportReport>('/api/v1/commercial-exclusions/import', {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ content }),
  })
}

export function fetchCommercialRelationships(view: string, search = ''): Promise<CommercialRelationshipPage> {
  const params = new URLSearchParams({ view })
  if (search.trim()) params.set('search', search.trim())
  return readJson<CommercialRelationshipPage>(`/api/v1/commercial-relationships?${params}`)
}

export function createCommercialRelationshipFromLead(companyKey: string, input: CommercialRelationshipInput): Promise<CommercialRelationship> {
  return readJson<CommercialRelationship>('/api/v1/commercial-relationships/from-lead', {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ company_key: companyKey, ...input }),
  })
}

export function updateCommercialRelationship(id: number, input: CommercialRelationshipInput): Promise<CommercialRelationship> {
  return readJson<CommercialRelationship>(`/api/v1/commercial-relationships/${id}`, {
    method: 'PATCH', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(input),
  })
}

export async function reopenCommercialOpportunity(id: number): Promise<void> {
  const response = await fetch(`${API_BASE_URL}/api/v1/commercial-relationships/${id}/reopen-opportunity`, { method: 'POST' })
  if (!response.ok) {
    let detail = `L’API locale a répondu avec une erreur (${response.status}).`
    try { detail = (await response.json() as { detail?: string }).detail || detail } catch { /* Keep fallback. */ }
    throw new Error(detail)
  }
}

export function fetchBraveUsage(): Promise<BraveUsage> {
  return readJson<BraveUsage>('/api/v1/brave-usage')
}

export function createSearchRun(input: SearchRunCreate): Promise<SearchRun> {
  return readJson<SearchRun>('/api/v1/search-runs', {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(input),
  })
}

export function fetchSearchRun(id: number): Promise<SearchRun> {
  return readJson<SearchRun>(`/api/v1/search-runs/${id}`)
}

export function stopSearchRun(id: number): Promise<SearchRun> {
  return readJson<SearchRun>(`/api/v1/search-runs/${id}/stop`, { method: 'POST' })
}

export function resumeSearchRun(id: number): Promise<SearchRun> {
  return readJson<SearchRun>(`/api/v1/search-runs/${id}/resume`, { method: 'POST' })
}

export function fetchSearchRunResults(id: number): Promise<CommercialLeadPage> {
  return readJson<CommercialLeadPage>(`/api/v1/search-runs/${id}/results`)
}

export function createJobOfferRefreshRun(): Promise<JobOfferRefreshRun> {
  return readJson<JobOfferRefreshRun>('/api/v1/job-offer-refresh-runs', { method: 'POST' })
}

export function fetchJobOfferRefreshRun(id: number): Promise<JobOfferRefreshRun> {
  return readJson<JobOfferRefreshRun>(`/api/v1/job-offer-refresh-runs/${id}`)
}

export function fetchActiveJobOfferRefreshRun(): Promise<JobOfferRefreshRun | null> {
  return readJson<JobOfferRefreshRun | null>('/api/v1/job-offer-refresh-runs/active')
}
