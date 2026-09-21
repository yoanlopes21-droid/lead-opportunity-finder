import type { BraveUsage, CommercialLeadPage } from './types'

const API_BASE_URL = 'http://127.0.0.1:8000'

async function readJson<T>(path: string): Promise<T> {
  const response = await fetch(`${API_BASE_URL}${path}`)
  if (!response.ok) throw new Error(`L’API locale a répondu avec une erreur (${response.status}).`)
  return response.json() as Promise<T>
}

export function fetchCommercialLeads(offset: number): Promise<CommercialLeadPage> {
  const params = new URLSearchParams({ department: '94', include_excluded: 'false', limit: '50', offset: String(offset) })
  return readJson<CommercialLeadPage>(`/api/v1/commercial-leads?${params}`)
}

export function fetchBraveUsage(): Promise<BraveUsage> {
  return readJson<BraveUsage>('/api/v1/brave-usage')
}
