import { useCallback, useEffect, useState } from 'react'
import { fetchRecentCommercialLeads } from '../api'
import type { RecentCommercialLeadPage, RecentLeadKind } from '../types'
import { LeadCard } from './LeadCard'

const windows = [
  { hours: 24, label: '24 h' },
  { hours: 48, label: '48 h' },
  { hours: 168, label: '7 jours' },
  { hours: 720, label: '30 jours' },
]
const kinds: Array<{ value: RecentLeadKind; label: string }> = [
  { value: 'all', label: 'Toutes' },
  { value: 'new_companies', label: 'Nouvelles entreprises' },
  { value: 'new_offers', label: 'Nouvelles offres' },
]

export function RecentLeads() {
  const [page, setPage] = useState<RecentCommercialLeadPage | null>(null)
  const [windowHours, setWindowHours] = useState(48)
  const [kind, setKind] = useState<RecentLeadKind>('all')
  const [isLoading, setIsLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [notice, setNotice] = useState<string | null>(null)

  const loadPage = useCallback(async (offset: number) => {
    setIsLoading(true); setError(null)
    try { setPage(await fetchRecentCommercialLeads(offset, windowHours, kind)) }
    catch (requestError) { setError(requestError instanceof Error ? requestError.message : 'Impossible de joindre l’API locale.') }
    finally { setIsLoading(false) }
  }, [kind, windowHours])

  useEffect(() => { void loadPage(0) }, [loadPage])

  const windowLabel = windows.find((item) => item.hours === windowHours)?.label ?? `${windowHours} h`
  const rangeStart = page && page.total > 0 ? page.offset + 1 : 0
  const rangeEnd = page ? Math.min(page.offset + page.items.length, page.total) : 0
  const canGoPrevious = Boolean(page && page.offset > 0)
  const canGoNext = Boolean(page && page.offset + page.items.length < page.total)

  return <section className="recent-page" aria-labelledby="recent-title">
    {notice && <p className="success-notice" role="status">{notice}</p>}
    <div className="section-heading"><div><p className="eyebrow">PREMIÈRE DÉTECTION LOCALE</p><h2 id="recent-title">Nouveautés</h2><p className="section-intro">Entreprises et besoins apparus récemment dans la base, toujours classés par score commercial.</p></div><button type="button" className="refresh-button" onClick={() => void loadPage(page?.offset ?? 0)} disabled={isLoading}>{isLoading && page ? 'Actualisation…' : 'Recharger la liste'}</button></div>
    <div className="recent-filters" aria-label="Filtres de nouveauté">
      <div><span>Fenêtre</span>{windows.map((item) => <button type="button" key={item.hours} className={windowHours === item.hours ? 'active' : ''} onClick={() => setWindowHours(item.hours)}>{item.label}</button>)}</div>
      <div><span>Type</span>{kinds.map((item) => <button type="button" key={item.value} className={kind === item.value ? 'active' : ''} onClick={() => setKind(item.value)}>{item.label}</button>)}</div>
    </div>
    {isLoading && !page && <div className="state-card" role="status">Chargement des nouveautés…</div>}
    {error && <div className="state-card error-state" role="alert"><p>{error}</p><button type="button" onClick={() => void loadPage(page?.offset ?? 0)}>Réessayer</button></div>}
    {page && !error && page.items.length === 0 && <div className="state-card"><h3>Aucune nouveauté</h3><p>Aucune opportunité éligible n’a été détectée dans cette fenêtre.</p></div>}
    {page && !error && page.items.length > 0 && <div className="lead-list">{page.items.map((lead) => <LeadCard key={lead.company_key} lead={lead} recentContext={{ latestNewOpportunityAt: lead.latest_new_opportunity_at, newOfferCount: lead.new_offer_count_in_window, isNewCompany: lead.is_new_company_in_window, newOfferIds: lead.new_offer_ids_in_window, windowLabel }} onRelationshipSaved={(message) => { setNotice(message); void loadPage(page.offset) }} />)}</div>}
    {page && !error && <nav className="pagination" aria-label="Pagination des nouveautés"><button type="button" onClick={() => void loadPage(Math.max(0, page.offset - page.limit))} disabled={!canGoPrevious || isLoading}>Page précédente</button><p>{rangeStart}–{rangeEnd} sur {page.total}</p><button type="button" onClick={() => void loadPage(page.offset + page.limit)} disabled={!canGoNext || isLoading}>Page suivante</button></nav>}
  </section>
}
