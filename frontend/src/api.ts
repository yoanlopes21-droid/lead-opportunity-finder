import type { BraveUsage, CommercialApproachPack, CommercialCatalogOfferSummary, CommercialExclusion, CommercialExclusionCreate, CommercialExclusionPage, CommercialLeadPage, CommercialRelationship, CommercialRelationshipInput, CommercialRelationshipPage, ExclusionImportPreview, ExclusionImportReport, InteractionCreate, InteractionHistory, InteractionSaved, JobOfferRefreshRun, JobSourceBoard, JobSourceBoardCreate, RecentCommercialLeadPage, RecentLeadKind, RecruitmentSignalPage, SearchRun, SearchRunCreate, SourceRefreshRun } from './types'

export const API_BASE_URL = 'http://127.0.0.1:8000'

export function fetchApproachPack(companyKey: string, department: string, offerCode?: string): Promise<CommercialApproachPack> {
  const params = new URLSearchParams({ department })
  if (offerCode) params.set('offer_code', offerCode)
  return readJson<CommercialApproachPack>(`/api/v1/commercial-leads/${encodeURIComponent(companyKey)}/approach-pack?${params}`)
}

export function fetchCommercialCatalogOffers(): Promise<CommercialCatalogOfferSummary[]> {
  return readJson<CommercialCatalogOfferSummary[]>('/api/v1/commercial-configuration/offers')
}

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

async function downloadXlsx(path: string): Promise<void> {
  const response = await fetch(`${API_BASE_URL}${path}`)
  if (!response.ok) {
    let detail = `L’export Excel a échoué (${response.status}).`
    try { detail = (await response.json() as { detail?: string }).detail || detail } catch { /* Keep fallback. */ }
    throw new Error(detail)
  }
  const disposition = response.headers.get('Content-Disposition') ?? ''
  const filename = disposition.match(/filename="?([^";]+)"?/i)?.[1] ?? 'lead-opportunity-finder.xlsx'
  const url = URL.createObjectURL(await response.blob())
  const link = document.createElement('a')
  link.href = url
  link.download = filename
  document.body.appendChild(link)
  link.click()
  link.remove()
  URL.revokeObjectURL(url)
}

export function fetchCommercialLeads(offset: number, includeExcluded = false): Promise<CommercialLeadPage> {
  const params = new URLSearchParams({ department: '94', include_excluded: String(includeExcluded), limit: '50', offset: String(offset) })
  return readJson<CommercialLeadPage>(`/api/v1/commercial-leads?${params}`)
}

export function downloadCommercialLeadsExcel(): Promise<void> {
  return downloadXlsx('/api/v1/commercial-leads/export.xlsx?department=94')
}

export function fetchRecentCommercialLeads(offset: number, windowHours: number, kind: RecentLeadKind): Promise<RecentCommercialLeadPage> {
  const params = new URLSearchParams({ department: '94', window_hours: String(windowHours), kind, limit: '50', offset: String(offset) })
  return readJson<RecentCommercialLeadPage>(`/api/v1/commercial-leads/recent?${params}`)
}

export function downloadRecentCommercialLeadsExcel(windowHours: number, kind: RecentLeadKind): Promise<void> {
  const params = new URLSearchParams({ department: '94', window_hours: String(windowHours), kind })
  return downloadXlsx(`/api/v1/commercial-leads/recent/export.xlsx?${params}`)
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

export function fetchInteractionHistory(companyKey: string): Promise<InteractionHistory> {
  return readJson<InteractionHistory>(`/api/v1/commercial-interactions/${encodeURIComponent(companyKey)}`)
}

export function createCommercialInteraction(input: InteractionCreate): Promise<InteractionSaved> {
  return readJson<InteractionSaved>('/api/v1/commercial-interactions', {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(input),
  })
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

export function downloadSearchRunExcel(id: number): Promise<void> {
  return downloadXlsx(`/api/v1/search-runs/${id}/export.xlsx`)
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

export function fetchJobSourceBoards(): Promise<JobSourceBoard[]> {
  return readJson<JobSourceBoard[]>('/api/v1/job-source-boards')
}

export function createJobSourceBoard(input: JobSourceBoardCreate): Promise<JobSourceBoard> {
  return readJson<JobSourceBoard>('/api/v1/job-source-boards', {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(input),
  })
}

export function updateJobSourceBoard(id: number, input: Partial<Pick<JobSourceBoard, 'display_name' | 'company_name_hint' | 'enabled'>>): Promise<JobSourceBoard> {
  return readJson<JobSourceBoard>(`/api/v1/job-source-boards/${id}`, {
    method: 'PATCH', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(input),
  })
}

export async function deleteJobSourceBoard(id: number): Promise<void> {
  const response = await fetch(`${API_BASE_URL}/api/v1/job-source-boards/${id}`, { method: 'DELETE' })
  if (!response.ok) throw new Error((await response.json() as { detail?: string }).detail || 'Suppression impossible.')
}

export function refreshJobSourceBoard(id: number): Promise<SourceRefreshRun> {
  return readJson<SourceRefreshRun>(`/api/v1/job-source-boards/${id}/refresh`, { method: 'POST' })
}

export function fetchSourceRefreshRun(id: number): Promise<SourceRefreshRun> {
  return readJson<SourceRefreshRun>(`/api/v1/job-source-boards/runs/${id}`)
}

export function createOpenWebRun(targetSignalCount: number, braveMaxRequests: number): Promise<SourceRefreshRun> {
  return readJson<SourceRefreshRun>('/api/v1/open-web-runs', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ target_signal_count: targetSignalCount, brave_max_requests: braveMaxRequests }),
  })
}

export function fetchLatestOpenWebRun(): Promise<SourceRefreshRun | null> {
  return readJson<SourceRefreshRun | null>('/api/v1/open-web-runs/latest')
}

export function fetchOpenWebRun(id: number): Promise<SourceRefreshRun> {
  return readJson<SourceRefreshRun>(`/api/v1/open-web-runs/${id}`)
}

export function stopOpenWebRun(id: number): Promise<SourceRefreshRun> {
  return readJson<SourceRefreshRun>(`/api/v1/open-web-runs/${id}/stop`, { method: 'POST' })
}

export function fetchRecruitmentSignals(status = 'new,review_needed'): Promise<RecruitmentSignalPage> {
  return readJson<RecruitmentSignalPage>(`/api/v1/recruitment-signals?status=${encodeURIComponent(status)}`)
}

export function promoteRecruitmentSignal(id: number): Promise<{ message: string }> {
  return readJson<{ message: string }>(`/api/v1/recruitment-signals/${id}/promote`, { method: 'POST' })
}

export function dismissRecruitmentSignal(id: number): Promise<{ message: string }> {
  return readJson<{ message: string }>(`/api/v1/recruitment-signals/${id}/dismiss`, { method: 'POST' })
}
