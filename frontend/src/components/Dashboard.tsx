import { useCallback, useEffect, useState } from 'react'
import { fetchCommercialLeads } from '../api'
import type { CommercialLeadPage } from '../types'
import { BraveUsageWidget } from './BraveUsageWidget'
import { LeadCard } from './LeadCard'

export function Dashboard() {
  const [page, setPage] = useState<CommercialLeadPage | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [isLoading, setIsLoading] = useState(true)

  const loadPage = useCallback(async (offset: number) => {
    setIsLoading(true); setError(null)
    try { setPage(await fetchCommercialLeads(offset)) }
    catch (requestError) { setError(requestError instanceof Error ? requestError.message : 'Impossible de joindre l’API locale.') }
    finally { setIsLoading(false) }
  }, [])

  useEffect(() => { void loadPage(0) }, [loadPage])

  const rangeStart = page && page.total > 0 ? page.offset + 1 : 0
  const rangeEnd = page ? Math.min(page.offset + page.items.length, page.total) : 0
  const canGoPrevious = Boolean(page && page.offset > 0)
  const canGoNext = Boolean(page && page.offset + page.items.length < page.total)

  return <>
    <section className="summary-grid" aria-label="Résumé des leads">
      <article><span>Leads prioritaires</span><strong>{page?.total ?? '—'}</strong><small>résultat de la recherche actuelle</small></article>
      <article><span>Département</span><strong>94</strong><small>Val-de-Marne</small></article>
      <article><span>Affichés</span><strong>{page?.items.length ?? '—'}</strong><small>sur cette page</small></article>
    </section>
    <BraveUsageWidget />
    <section className="lead-section" aria-labelledby="lead-list-title"><div className="section-heading"><div><p className="eyebrow">LISTE PRIORISÉE</p><h2 id="lead-list-title">Opportunités à contacter</h2></div>
      <button type="button" className="refresh-button" onClick={() => void loadPage(page?.offset ?? 0)} disabled={isLoading}>{isLoading && page ? 'Actualisation…' : 'Actualiser'}</button>
    </div>
      {isLoading && !page && <div className="state-card" role="status">Chargement des opportunités locales…</div>}
      {error && <div className="state-card error-state" role="alert"><p>{error}</p><button type="button" onClick={() => void loadPage(page?.offset ?? 0)}>Réessayer</button></div>}
      {page && !error && page.items.length === 0 && <div className="state-card"><h3>Aucun lead à afficher</h3><p>Aucune opportunité ne correspond à cette recherche pour le moment.</p></div>}
      {page && !error && page.items.length > 0 && <div className="lead-list">{page.items.map((lead) => <LeadCard key={lead.company_key} lead={lead} />)}</div>}
    </section>
    {page && !error && <nav className="pagination" aria-label="Pagination des leads"><button type="button" onClick={() => void loadPage(Math.max(0, page.offset - page.limit))} disabled={!canGoPrevious || isLoading}>Page précédente</button><p>{rangeStart}–{rangeEnd} sur {page.total}</p><button type="button" onClick={() => void loadPage(page.offset + page.limit)} disabled={!canGoNext || isLoading}>Page suivante</button></nav>}
  </>
}
