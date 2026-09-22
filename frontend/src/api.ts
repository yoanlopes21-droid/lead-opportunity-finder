import type { BraveUsage, CommercialLeadPage, JobOfferRefreshRun, SearchRun, SearchRunCreate } from './types'

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

export function fetchCommercialLeads(offset: number): Promise<CommercialLeadPage> {
  const params = new URLSearchParams({ department: '94', include_excluded: 'false', limit: '50', offset: String(offset) })
  return readJson<CommercialLeadPage>(`/api/v1/commercial-leads?${params}`)
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
